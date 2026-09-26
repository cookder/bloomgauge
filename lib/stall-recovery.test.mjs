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

test('a recent episode stays visible for a day and says whether work came back', () => {
  const events = [
    event(3600, 'stall-restart'),
    event(4000, 'stall-nudge', 'Test request … failed (timeout).'),
    event(4100, 'stall-probe'),
  ];
  const flowing = stallView(
    { status: 'ok', silenceSeconds: 20, at: now, fresh: true },
    events,
    now,
  );
  assert.equal(flowing.state, 'recent');
  assert.equal(flowing.headline, 'Work is arriving again.');
  assert.deepEqual(
    flowing.steps.map((s) => s.state),
    ['done', 'done', 'upcoming', 'upcoming'],
  );
  assert.equal(flowing.since, now - 4100);
  const gaveUp = stallView(
    { status: 'inactive', at: now, fresh: true },
    [...events, event(1000, 'stall-escape'), event(500, 'stall-hold')],
    now,
  );
  assert.equal(gaveUp.headline, 'Bloomkeeper stopped trying after these steps.');
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
    { status: 'ok', silenceSeconds: 30, at: now, fresh: true },
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

test('trickling work after a stall is not called a lack of steady work', () => {
  const events = [event(3600, 'stall-probe')];
  assert.equal(
    stallView(
      { status: 'quiet', silenceSeconds: 57, at: now, fresh: true },
      events,
      now,
    ).headline,
    'Work is arriving again.',
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
