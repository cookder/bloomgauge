import test from 'node:test';
import assert from 'node:assert/strict';
import { guideSections } from './guide-content.ts';

const section = (id) => guideSections.find((s) => s.id === id);
const text = (s) =>
  [s.summary, s.note ?? '', ...s.steps.map((x) => `${x.title ?? ''} ${x.text}`)].join(' ');

test('the Guide describes the manager as the default, legacy only as the older mode', () => {
  const does = section('what-optimizer-does');
  assert.deepEqual(
    does.steps.map((s) => s.title),
    [
      'Holds your best model',
      'Recovers by itself',
      'Moves only on strong evidence',
      'The older demand-following mode',
    ],
  );
  // Spike trials, the gpt-oss fallback and pausing after a failed load are legacy only.
  for (const s of does.steps.slice(0, 3))
    assert.doesNotMatch(s.text, /spike|gpt-oss|pause|Learning/i);
  assert.match(does.steps[3].text, /gpt-oss/);
  assert.doesNotMatch(does.note, /boost/i);
  const on = section('optimizer').steps.find((s) => s.title === 'Turn it on');
  assert.match(on.text, /^Choose Manager on\./);
  assert.match(on.text, /older demand-following mode.*3-day Learning boost/);
  assert.match(section('gathering').summary, /^Older demand-following mode only/);
  assert.match(text(section('manual')), /choose Off/);
});
