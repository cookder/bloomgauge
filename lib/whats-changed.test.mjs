import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  shouldShowWhatsChanged,
  validWhatsChangedStatus,
  readLocalSeen,
  markLocalSeen,
  whatsChangedId,
  managerChanges,
  smallerChanges,
  feedbackAction,
  earlierUseKeys,
} from './whats-changed.ts';
import { introSeenKey } from './optimizer-intro.ts';
import { releaseSeenKey } from './release-notes.ts';

const server = (returning, seen = []) => ({ available: true, returning, seen });
const offline = { available: false, returning: null, seen: [] };

test('an upgraded Mac sees the card once, on any device', () => {
  const base = { localSeen: [], earlierUseHere: false };
  assert.equal(shouldShowWhatsChanged({ ...base, server: server(true) }), true);
  // Closed on the phone: the Mac's record wins even in a fresh browser.
  assert.equal(
    shouldShowWhatsChanged({ ...base, server: server(true, [whatsChangedId]) }),
    false,
  );
  // Closed in this browser but the Mac could not save it.
  assert.equal(
    shouldShowWhatsChanged({
      server: server(true),
      localSeen: [whatsChangedId],
      earlierUseHere: true,
    }),
    false,
  );
  // A later note gets its own id.
  assert.equal(
    shouldShowWhatsChanged({
      ...base,
      id: 'next-note',
      server: server(true, [whatsChangedId]),
    }),
    true,
  );
});

test('brand-new installs skip it; the first-On explainer covers the manager', () => {
  assert.equal(
    shouldShowWhatsChanged({
      server: server(false),
      localSeen: [],
      earlierUseHere: true,
    }),
    false,
  );
});

test('without the Mac, only a browser an earlier version used shows it', () => {
  for (const s of [null, offline]) {
    assert.equal(
      shouldShowWhatsChanged({ server: s, localSeen: [], earlierUseHere: true }),
      true,
    );
    assert.equal(
      shouldShowWhatsChanged({ server: s, localSeen: [], earlierUseHere: false }),
      false,
    );
  }
  assert.deepEqual(earlierUseKeys, [releaseSeenKey, introSeenKey]);
});

test('status and local seen lists are read defensively', () => {
  assert.equal(validWhatsChangedStatus(server(true)), true);
  assert.equal(validWhatsChangedStatus(offline), true);
  assert.equal(validWhatsChangedStatus({ ...server(true), seen: ['A B'] }), false);
  assert.equal(validWhatsChangedStatus({ available: true, seen: [] }), false);
  assert.equal(validWhatsChangedStatus(null), false);
  assert.deepEqual(readLocalSeen('not json'), []);
  assert.deepEqual(readLocalSeen('{"a":1}'), []);
  assert.deepEqual(readLocalSeen(null), []);
  const once = markLocalSeen(null, whatsChangedId);
  assert.deepEqual(readLocalSeen(markLocalSeen(once, whatsChangedId)), [
    whatsChangedId,
  ]);
});

test('key copy: honest numbers, short bullets, a way to send ideas', () => {
  const all = managerChanges.join(' ');
  assert.match(all, /now a manager/);
  assert.match(all, /13–16% less than just staying on Gemma/);
  assert.match(all, /instead of switching itself off/);
  assert.match(all, /at least 5 Macs like yours/);
  assert.ok(managerChanges.length >= 3 && managerChanges.length <= 5);
  assert.ok(smallerChanges.length >= 5 && smallerChanges.length <= 8);
  for (const line of [...managerChanges, ...smallerChanges])
    assert.ok(line.length <= 200, line);
  assert.equal(feedbackAction, 'Suggest a feature or send feedback');
  const card = readFileSync(
    new URL('../components/dashboard/whats-changed.tsx', import.meta.url),
    'utf8',
  );
  assert.match(card, /navigate\('support'\)/);
  assert.match(card, /'X-Bloom-Action': 'whats-changed'/);
  assert.doesNotMatch(card, /#[0-9a-f]{3,6}\b/i);
  const tab = readFileSync(
    new URL('../components/dashboard/optimizer-tab.tsx', import.meta.url),
    'utf8',
  );
  assert.match(tab, /<AppScreen name="test">\s*<WhatsChanged \/>/);
  const control = readFileSync(
    new URL('../components/dashboard/optimizer-control.tsx', import.meta.url),
    'utf8',
  );
  assert.match(control, /onClick=\{showWhatsChanged\}>\s*What’s changed/);
});
