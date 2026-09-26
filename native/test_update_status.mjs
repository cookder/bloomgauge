import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import ts from 'typescript';
const source = ts.transpileModule(
  fs.readFileSync(new URL('../lib/update-status.ts', import.meta.url), 'utf8'),
  {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  },
).outputText;
const { validUpdateStatus } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
);
const state = () => ({
  requestId: null,
  available: true,
  installedVersion: '1.36.7',
  automaticChecks: false,
  canCheck: true,
  checking: false,
  lastCheck: null,
  status: 'idle',
  error: null,
});
test('native settings and asynchronous update outcomes validate without implying opt-in', () => {
  for (const status of [
    'idle',
    'checking',
    'update-available',
    'up-to-date',
    'error',
    'blocked',
  ]) {
    assert.equal(validUpdateStatus({ ...state(), status }), true);
  }
  assert.equal(validUpdateStatus({ ...state(), available: false }), true);
});
test('malformed native messages cannot render invalid dates or controls', () => {
  for (const patch of [
    { lastCheck: NaN },
    { lastCheck: Infinity },
    { lastCheck: -1 },
    { lastCheck: 1e15 },
    { available: 'true' },
    { automaticChecks: 'yes' },
    { canCheck: 1 },
    { checking: null },
    { installedVersion: null },
    { installedVersion: 'a'.repeat(100) },
    { requestId: {} },
    { status: 'anything' },
    { error: { message: 'broken' } },
  ]) {
    assert.equal(
      validUpdateStatus({ ...state(), ...patch }),
      false,
      JSON.stringify(patch),
    );
  }
  assert.equal(validUpdateStatus(null), false);
});
