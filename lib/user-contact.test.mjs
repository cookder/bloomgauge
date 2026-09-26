import test from 'node:test';
import assert from 'node:assert/strict';
import { contactKind, validContactStatus } from './user-contact.ts';
test('accepts emails and @ Slack handles, matching the backend', () => {
  assert.equal(contactKind('andrew@example.com'), 'email');
  assert.equal(contactKind('@copyfax'), 'slack');
  assert.equal(contactKind('@Andrew S'), 'slack');
  for (const bad of [
    '',
    'andrew',
    '@',
    'a@b',
    '@bad@x',
    '@x\n',
    'x'.repeat(250) + '@example.com',
    ' @x',
  ])
    assert.equal(contactKind(bad), null, bad);
});
test('status must be internally consistent', () => {
  assert.ok(
    validContactStatus({
      schema: 1,
      contact: null,
      kind: null,
      savedAt: null,
      canChange: true,
    }),
  );
  assert.ok(
    validContactStatus({
      schema: 1,
      contact: '@copyfax',
      kind: 'slack',
      savedAt: 1790000000,
      canChange: false,
    }),
  );
  assert.ok(
    !validContactStatus({
      schema: 1,
      contact: '@copyfax',
      kind: 'email',
      savedAt: 1,
      canChange: true,
    }),
  );
  assert.ok(
    !validContactStatus({
      schema: 1,
      contact: null,
      kind: 'email',
      savedAt: null,
      canChange: true,
    }),
  );
  assert.ok(!validContactStatus(null));
});
