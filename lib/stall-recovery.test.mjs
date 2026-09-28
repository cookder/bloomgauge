import test from 'node:test';
import assert from 'node:assert/strict';
import { stallView, validStallSnapshot } from './stall-recovery.ts';

const now = 1_790_000_000;
const stalled = (over = {}) => ({
  status: 'stalled',
  step: null,
  reason: 'Waiting to see whether work resumes after the probe.',
  model: 'gemma-4-26b',
  silenceSeconds: 420,
  requiredSeconds: 300,
  baselineJobsPerMinute: 9.4,
  demandHeld: true,
  episodeStart: now - 420,
  taken: ['probe'],
  at: now,
  fresh: true,
  ...over,
});
const event = (ago, kind, detail = '', model = 'gemma-4-26b') => ({
  at: now - ago,
  kind,
  model,
  detail,
  downtime: 0,
});

test('an active stall lists the steps taken, the nudge result and what comes next', () => {
  const view = stallView(
    stalled(),
    [
      event(120, 'stall-probe', 'Work stopped abruptly…'),
      event(90, 'stall-nudge', 'Test request through Darkbloom was served.'),
      event(9000, 'stall-hold', 'old episode'),
    ],
    now,
  );
  assert.equal(view.state, 'active');
  assert.equal(view.model, 'gemma-4-26b');
  assert.match(
    view.headline,
    /No work for 7 min after about 9 jobs a minute\. Network demand for it held\./,
  );
  assert.deepEqual(
    view.steps.map((s) => s.state),
    ['done', 'upcoming', 'upcoming', 'upcoming'],
  );
  assert.equal(
    view.steps[0].note,
    'Test request through Darkbloom was served.',
  );
});

test('a step the snapshot took before its event arrived still shows as done', () => {
  const view = stallView(
    stalled({
      taken: ['probe'],
      nudge: { kind: 'local', ok: false, code: 'timeout' },
    }),
    [],
    now,
  );
  assert.equal(view.steps[0].state, 'done');
  assert.equal(view.steps[0].note, 'Failed (timeout).');
});

test('when demand fell, earlier steps read as skipped', () => {
  const view = stallView(
    stalled({ demandHeld: false, taken: ['escape'] }),
    [event(60, 'stall-escape', 'Moving on')],
    now,
  );
  assert.deepEqual(
    view.steps.map((s) => s.state),
    ['skipped', 'skipped', 'done', 'upcoming'],
  );
  assert.match(view.headline, /fell too/);
});

test('a recent episode stays visible for a day until work comes back', () => {
  const events = [
    event(3600, 'stall-restart'),
    event(4000, 'stall-nudge', 'Test request … failed (timeout).'),
    event(4100, 'stall-probe'),
  ];
  const quiet = stallView(
    { status: 'quiet', silenceSeconds: 1800, at: now, fresh: true },
    events,
    now,
  );
  assert.equal(quiet.state, 'recent');
  assert.equal(quiet.headline, 'No steady work right now.');
  assert.deepEqual(
    quiet.steps.map((s) => s.state),
    ['done', 'done', 'upcoming', 'upcoming'],
  );
  assert.equal(quiet.since, now - 4100);
  // Work is arriving again: the episode is over and the card goes away.
  assert.equal(
    stallView(
      { status: 'ok', silenceSeconds: 20, at: now, fresh: true },
      events,
      now,
    ),
    null,
  );
  const gaveUp = stallView(
    { status: 'inactive', at: now, fresh: true },
    [...events, event(1000, 'stall-escape'), event(500, 'stall-hold')],
    now,
  );
  assert.equal(
    gaveUp.headline,
    'Bloomkeeper stopped trying after these steps.',
  );
});

test('a model that became ready after the stall ends the card', () => {
  const stale = {
    status: 'ok',
    silenceSeconds: 0,
    at: now - 9000,
    fresh: false,
  };
  const events = [event(1100, 'stall-probe'), event(1000, 'stall-escape')];
  assert.equal(stallView(stale, events, now).state, 'recent');
  for (const later of [
    event(900, 'switched', 'gpt-oss-20b is warm and ready.', 'gpt-oss-20b'),
    event(100, 'recovered', 'Restored gemma.'),
    event(100, 'resumed', 'Saved demand plan resumed.'),
    event(
      100,
      'manager',
      'gemma-4-26b is ready again. Automatic control continues.',
    ),
  ])
    assert.equal(stallView(stale, [...events, later], now), null, later.kind);
  // Other notes after the stall do not end it.
  assert.equal(
    stallView(
      stale,
      [...events, event(100, 'manager', 'Home model is now gemma.')],
      now,
    ).state,
    'recent',
  );
});

test('under the manager the ladder holds the home model instead of trying another model', () => {
  const view = stallView(
    stalled({ taken: ['probe', 'restart'] }),
    [event(120, 'stall-probe'), event(60, 'stall-restart')],
    now,
    { manager: true },
  );
  assert.deepEqual(
    view.steps.map((s) => s.step),
    ['probe', 'restart', 'hold'],
  );
  assert.equal(view.steps[2].label, 'Hold the home model and notify you');
  assert.ok(!view.steps.some((s) => /another model/i.test(s.label)));
  const held = stallView(
    { status: 'inactive', at: now, fresh: true },
    [event(900, 'stall-probe'), event(600, 'stall-hold')],
    now,
    { manager: true },
  );
  assert.equal(
    held.headline,
    'Bloomkeeper held the home model after these steps.',
  );
  // An escape an older (legacy) episode really took is still reported as done.
  const legacy = stallView(
    { status: 'inactive', at: now, fresh: true },
    [event(900, 'stall-probe'), event(600, 'stall-escape')],
    now,
    { manager: true },
  );
  assert.deepEqual(
    legacy.steps.map((s) => [s.step, s.state]),
    [
      ['probe', 'done'],
      ['restart', 'skipped'],
      ['escape', 'done'],
      ['hold', 'upcoming'],
    ],
  );
});

test('two episodes close together show only the latest one', () => {
  const events = [
    event(9000, 'stall-probe', 'first'),
    event(8000, 'stall-restart', 'first'),
    event(7000, 'stall-escape', 'first'),
    event(1200, 'stall-probe', 'second'),
    event(1150, 'stall-nudge', 'Test request through Darkbloom was served.'),
  ];
  const view = stallView(
    { status: 'quiet', silenceSeconds: 900, at: now, fresh: true },
    events,
    now,
  );
  assert.equal(view.since, now - 1200);
  assert.deepEqual(
    view.steps.map((s) => s.state),
    ['done', 'upcoming', 'upcoming', 'upcoming'],
  );
  assert.equal(
    view.steps[0].note,
    'Test request through Darkbloom was served.',
  );
});

test('trickling work after a stall ends the card; a long silence keeps it', () => {
  const events = [event(3600, 'stall-probe')];
  assert.equal(
    stallView(
      { status: 'quiet', silenceSeconds: 57, at: now, fresh: true },
      events,
      now,
    ),
    null,
  );
  assert.equal(
    stallView(
      { status: 'quiet', silenceSeconds: 1800, at: now, fresh: true },
      events,
      now,
    ).headline,
    'No steady work right now.',
  );
  assert.equal(
    stallView({ status: 'quiet', at: now, fresh: true }, events, now).headline,
    'No steady work right now.',
  );
});

test('nothing to report hides the panel', () => {
  assert.equal(
    stallView(
      { status: 'ok', at: now, fresh: true },
      [event(60, 'switching')],
      now,
    ),
    null,
  );
  assert.equal(
    stallView(
      { status: 'ok', at: now, fresh: true },
      [event(90000, 'stall-probe')],
      now,
    ),
    null,
  );
  assert.equal(stallView(undefined, undefined, now), null);
});

test('a stale or malformed snapshot is never shown as an active stall', () => {
  assert.equal(stallView(stalled({ fresh: false }), [], now), null);
  assert.equal(validStallSnapshot(stalled({ taken: 'probe' })), false);
  assert.equal(validStallSnapshot(stalled({ silenceSeconds: 'x' })), false);
  assert.equal(
    validStallSnapshot({ status: 'inactive', at: now, fresh: true }),
    true,
  );
  assert.equal(
    stallView(stalled({ at: null }), [event(60, 'stall-probe')], now).state,
    'recent',
  );
});
test('a stall episode that ended hours ago is history, not a card (Sep 27 5:44 AM seen at 6 PM)', () => {
  const now = 1_790_550_000;
  const events = [
    { at: now - 12 * 3600, kind: 'stall-escape', model: 'gemma-4-26b-qat-4bit', detail: 'Work stopped.' },
  ];
  // The live snapshot carries no status when no stall is being tracked.
  assert.equal(stallView({ at: now, fresh: true }, events, now, { manager: true }), null);
  assert.equal(stallView({ status: 'inactive', at: now, fresh: true }, events, now), null);
});
