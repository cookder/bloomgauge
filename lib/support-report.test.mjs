import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import ts from 'typescript';

// Load the actual TypeScript without a build or application/browser startup.
// A unique URL gives each store test independent module state and boot time.
let revision = 0;
const source = async (name) => readFile(new URL(name, import.meta.url), 'utf8');
const moduleUrl = (code) =>
  'data:text/javascript;base64,' +
  Buffer.from(code).toString('base64') +
  '#' +
  revision++;
const compile = (code) =>
  ts.transpileModule(code, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ESNext,
    },
  }).outputText;
const issuesCode = compile(await source('support-issues.ts'));
const reportCode = compile(await source('support-report.ts'));
const issuesUrl = moduleUrl(issuesCode);
const {
  validSupportPreview,
  validSupportReceipt,
  supportRequest,
  SupportRequestError,
  quickSupportReport,
  readAutoSend,
  writeAutoSend,
} = await import(
  moduleUrl(
    reportCode.replace(
      /(['"])\.\/support-issues\1/g,
      JSON.stringify(issuesUrl),
    ),
  )
);
const NOW = Date.parse('2026-09-20T12:00:00.000Z');
const DAY = 86400000;
const STORAGE = 'bloom-support-prompts-v1';
const fields = () => ({
  category: 'manual',
  context: 'help',
  description: '',
  contact: '',
});
function preview(input = fields()) {
  return {
    report: {
      schema: 1,
      id: '0123456789abcdef0123456789abcdef',
      generatedAt: new Date(NOW).toISOString(),
      appVersion: '1.36.8',
      surface: 'mac',
      ...input,
      diagnostics: {
        available: true,
        osMajor: 26,
        chipFamily: 'M4 Max',
        memoryBand: '65-128',
        providerOnline: true,
        providerVersion: '0.7.1',
        optimizerMode: 'observe',
        optimizerStatus: 'ready',
        failureCode: 'none',
        recoveryCode: 'none',
        sources: [
          { name: 'earnings', status: 'ok', error: 'none' },
          { name: 'monitor', status: 'ready', error: 'none' },
          { name: 'network', status: 'stale', error: 'timeout' },
        ],
      },
    },
    reviewToken: 'ab'.repeat(32),
  };
}
function setAt(value, path, replacement) {
  const keys = path.split('.');
  let target = value;
  for (const key of keys.slice(0, -1)) target = target[key];
  target[keys.at(-1)] = replacement;
}
async function store(t, initial = {}) {
  let now = NOW;
  const saved = new Map(Object.entries(initial));
  const original = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, String(value)),
    },
  });
  t.after(() => {
    if (original) Object.defineProperty(globalThis, 'localStorage', original);
    else delete globalThis.localStorage;
  });
  t.mock.method(Date, 'now', () => now);
  const load = () => import(moduleUrl(issuesCode));
  return {
    api: await load(),
    reload: load,
    saved,
    advance: (ms) => {
      now += ms;
    },
    setTime: (value) => {
      now = value;
    },
  };
}
function fakeFetch(t, implementation) {
  t.mock.method(globalThis, 'fetch', implementation);
}

test('accepts minimal reports, both surfaces, explicit user text and unavailable diagnostics', () => {
  for (const surface of ['mac', 'phone']) {
    const input = {
      ...fields(),
      description: 'I saw <b>this</b>\nPlease inspect.',
      contact: '@my-handle',
    };
    const value = preview(input);
    value.report.surface = surface;
    assert.equal(validSupportPreview(value, input, NOW), true);
  }
  const value = preview();
  Object.assign(value.report.diagnostics, {
    available: false,
    osMajor: 0,
    chipFamily: 'Other',
    memoryBand: 'unknown',
    providerOnline: null,
    providerVersion: 'unknown',
    optimizerMode: 'unknown',
    optimizerStatus: 'unknown',
    failureCode: 'unknown',
    recoveryCode: 'unknown',
    sources: [],
  });
  assert.equal(validSupportPreview(value, fields(), NOW), true);
});

test('rejects private or unexpected properties throughout the response envelope', async (t) => {
  for (const [name, path] of [
    ['envelope', ''],
    ['report', 'report'],
    ['diagnostics', 'report.diagnostics'],
    ['source', 'report.diagnostics.sources.0'],
  ]) {
    await t.test(name, () => {
      const value = preview();
      const target = path
        ? path.split('.').reduce((item, key) => item[key], value)
        : value;
      target.rawError =
        'account=private; /Users/person; https://private.example/token';
      assert.equal(validSupportPreview(value, fields(), NOW), false);
    });
  }
  const fullExport = {
    schema: 'bloom-diagnostics-v1',
    privacy: { earningsIncluded: false },
    optimizer: { recentDecisions: [] },
  };
  assert.equal(
    validSupportPreview(
      { report: fullExport, reviewToken: 'ab'.repeat(32) },
      fields(),
      NOW,
    ),
    false,
  );
});

test('requires every field and a strict object envelope', async (t) => {
  for (const invalid of [null, undefined, [], {}, 'report', false])
    assert.equal(validSupportPreview(invalid, fields(), NOW), false);
  for (const path of [
    'reviewToken',
    'report.schema',
    'report.id',
    'report.generatedAt',
    'report.appVersion',
    'report.surface',
    'report.category',
    'report.context',
    'report.description',
    'report.contact',
    'report.diagnostics',
    'report.diagnostics.available',
    'report.diagnostics.providerOnline',
    'report.diagnostics.sources.0.error',
  ]) {
    await t.test('missing ' + path, () => {
      const value = preview();
      const keys = path.split('.');
      let target = value;
      for (const key of keys.slice(0, -1)) target = target[key];
      delete target[keys.at(-1)];
      assert.equal(validSupportPreview(value, fields(), NOW), false);
    });
  }
});

test('rejects malformed values rather than coercing or exposing diagnostic free text', async (t) => {
  const cases = [
    ['reviewToken', 'A'.repeat(64)],
    ['reviewToken', 'a'.repeat(63)],
    ['reviewToken', 7],
    ['report.schema', '1'],
    ['report.id', 'A'.repeat(32)],
    ['report.id', 'a'.repeat(31)],
    ['report.id', null],
    ['report.category', 'crash'],
    ['report.context', '/Users/person/private'],
    ['report.surface', 'desktop'],
    ['report.appVersion', 'version 1'],
    ['report.appVersion', null],
    ['report.description', {}],
    ['report.contact', 42],
    ['report.diagnostics.available', 'true'],
    ['report.diagnostics.osMajor', -1],
    ['report.diagnostics.osMajor', 100],
    ['report.diagnostics.osMajor', 26.5],
    ['report.diagnostics.osMajor', '26'],
    ['report.diagnostics.osMajor', NaN],
    ['report.diagnostics.chipFamily', 'Apple M4 Max SN123'],
    ['report.diagnostics.chipFamily', 'M123'],
    ['report.diagnostics.memoryBand', '128 GB'],
    ['report.diagnostics.providerOnline', 'true'],
    ['report.diagnostics.providerVersion', 'private-build-path'],
    ['report.diagnostics.optimizerMode', 'my custom mode'],
    ['report.diagnostics.optimizerStatus', 'private error'],
    ['report.diagnostics.failureCode', 'private stack'],
    ['report.diagnostics.recoveryCode', 'private path'],
    ['report.diagnostics.sources', {}],
    ['report.diagnostics.sources', [null]],
    ['report.diagnostics.sources.0.name', 'account'],
    ['report.diagnostics.sources.0.status', 'private status'],
    ['report.diagnostics.sources.0.error', 'raw error'],
  ];
  for (const [index, [path, invalid]] of cases.entries())
    await t.test(index + ' ' + path, () => {
      const value = preview();
      setAt(value, path, invalid);
      assert.equal(validSupportPreview(value, fields(), NOW), false);
    });
  const value = preview();
  value.report.diagnostics.sources.push({
    ...value.report.diagnostics.sources[0],
  });
  assert.equal(validSupportPreview(value, fields(), NOW), false);
});

test('accepts only exactly reviewed user fields and never auto-fills contact', async (t) => {
  for (const key of ['category', 'context', 'description', 'contact'])
    await t.test(key, () => {
      const value = preview();
      value.report[key] = {
        category: 'ui',
        context: 'overview',
        description: 'Added by collector',
        contact: 'autofilled@example.com',
      }[key];
      assert.equal(validSupportPreview(value, fields(), NOW), false);
    });
  const input = {
    ...fields(),
    description: '  exact whitespace\n',
    contact: ' @person ',
  };
  assert.equal(validSupportPreview(preview(input), input, NOW), true);
});

test('rejects duplicate diagnostics source names even when each entry is valid', () => {
  const value = preview();
  value.report.diagnostics.sources = [
    { name: 'earnings', status: 'ok', error: 'none' },
    { name: 'earnings', status: 'stale', error: 'timeout' },
  ];
  assert.equal(validSupportPreview(value, fields(), NOW), false);
  value.report.diagnostics.sources[1].name = 'monitor';
  assert.equal(validSupportPreview(value, fields(), NOW), true);
});

test('enforces character limits by Unicode code point and the serialized UTF-8 limit', () => {
  const input = {
    ...fields(),
    description: '😀'.repeat(2000),
    contact: '😀'.repeat(254),
  };
  assert.equal(validSupportPreview(preview(input), input, NOW), true);
  for (const key of ['description', 'contact']) {
    const tooLong = { ...input, [key]: input[key] + '😀' };
    assert.equal(validSupportPreview(preview(tooLong), tooLong, NOW), false);
  }
  const escaped = {
    ...fields(),
    description: '\0'.repeat(2000),
    contact: '😀'.repeat(254),
  };
  const value = preview(escaped);
  assert.ok(Buffer.byteLength(JSON.stringify(value.report), 'utf8') > 12288);
  assert.equal(validSupportPreview(value, escaped, NOW), false);
});

test('timestamp window includes its boundaries and requires UTC ISO syntax', () => {
  for (const offset of [-DAY, 300000]) {
    const value = preview();
    value.report.generatedAt = new Date(NOW + offset).toISOString();
    assert.equal(validSupportPreview(value, fields(), NOW), true);
  }
  for (const invalid of [
    new Date(NOW - DAY - 1).toISOString(),
    new Date(NOW + 300001).toISOString(),
    '2026-09-20',
    '2026-09-20T07:00:00-05:00',
    'not a date',
    '2026-09-20T25:00:00Z',
  ]) {
    const value = preview();
    value.report.generatedAt = invalid;
    assert.equal(validSupportPreview(value, fields(), NOW), false);
  }
  for (const valid of [
    '2026-09-20T12:00:00Z',
    '2026-09-20T12:00:00.123456Z',
    '2026-09-20T12:00:00+00:00',
  ]) {
    const value = preview();
    value.report.generatedAt = valid;
    assert.equal(validSupportPreview(value, fields(), NOW), true);
  }
});

test('rejects impossible calendar dates even when Date.parse normalizes them', () => {
  const value = preview();
  value.report.generatedAt = '2026-02-30T12:00:00.000Z';
  assert.equal(
    validSupportPreview(value, fields(), Date.parse('2026-03-02T12:00:00Z')),
    false,
  );
});

test('only an exact matching sent receipt confirms submission', () => {
  const id = preview().report.id;
  assert.equal(validSupportReceipt({ status: 'sent', reportId: id }, id), true);
  for (const value of [
    null,
    {},
    [],
    { status: 'queued', reportId: id },
    { status: 'sent', reportId: 'f'.repeat(32) },
    { status: 'sent', reportId: id, details: 'raw server text' },
    { status: 'sent' },
    { reportId: id },
  ])
    assert.equal(validSupportReceipt(value, id), false);
});

test('supportRequest uses the fixed local route and exact reviewed send body', async (t) => {
  const body = {
    reportId: preview().report.id,
    reviewToken: preview().reviewToken,
    confirmed: true,
  };
  const calls = [];
  fakeFetch(t, async (url, options) => {
    calls.push({ url, options });
    return new Response(
      JSON.stringify({ status: 'sent', reportId: body.reportId }),
    );
  });
  assert.deepEqual(
    await supportRequest('send', body, new AbortController().signal, 100),
    { status: 'sent', reportId: body.reportId },
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/support/send');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.cache, 'no-store');
  assert.deepEqual(calls[0].options.headers, {
    'Content-Type': 'application/json',
    'X-Bloom-Action': 'support',
  });
  assert.deepEqual(JSON.parse(calls[0].options.body), body);
  assert.equal(calls[0].options.signal.aborted, false);
});

test('supportRequest preview passes only supplied review fields to its local route', async (t) => {
  fakeFetch(t, async (url, options) => {
    assert.equal(url, '/api/support/preview');
    assert.deepEqual(JSON.parse(options.body), fields());
    return new Response(JSON.stringify(preview()));
  });
  assert.deepEqual(
    await supportRequest(
      'preview',
      fields(),
      new AbortController().signal,
      100,
    ),
    preview(),
  );
});

test('supportRequest parses non-OK JSON once and preserves only allowlisted error statuses', async (t) => {
  for (const status of [
    'busy',
    'expired',
    'rate_limited',
    'unavailable',
    'preview_limit',
  ])
    await t.test(status, async (st) => {
      let count = 0,
        reads = 0;
      const raw = 'SECRET_ACCOUNT /Users/private https://private.example/token';
      fakeFetch(st, async () => {
        count++;
        return {
          ok: false,
          text: async () => {
            reads++;
            return JSON.stringify({
              status,
              error: raw,
              message: raw,
              stack: raw,
              details: { token: raw },
            });
          },
        };
      });
      await assert.rejects(
        supportRequest('send', {}, new AbortController().signal, 100),
        (error) => {
          assert.ok(error instanceof SupportRequestError);
          assert.equal(error.status, status);
          assert.equal(error.message, 'unconfirmed');
          assert.equal(error.cause, undefined);
          assert.equal(error.details, undefined);
          assert.equal(JSON.stringify(error).includes(raw), false);
          assert.equal(String(error.stack).includes(raw), false);
          assert.deepEqual(Object.keys(error), ['status']);
          return true;
        },
      );
      assert.equal(count, 1);
      assert.equal(reads, 1);
    });
});

test('supportRequest treats unknown or malformed status values as unconfirmed', async (t) => {
  for (const [index, value] of [
    null,
    [],
    42,
    'busy',
    {},
    { status: 'sent' },
    { status: ' busy' },
    { status: 'PRIVATE_RAW_ERROR' },
    { status: 429 },
    { status: { message: 'private' } },
  ].entries())
    await t.test(String(index), async (st) => {
      fakeFetch(
        st,
        async () => new Response(JSON.stringify(value), { status: 503 }),
      );
      await assert.rejects(
        supportRequest('preview', {}, new AbortController().signal, 100),
        (error) => {
          assert.ok(error instanceof SupportRequestError);
          assert.equal(error.status, 'unconfirmed');
          assert.equal(error.message, 'unconfirmed');
          assert.equal(
            JSON.stringify(error).includes('PRIVATE_RAW_ERROR'),
            false,
          );
          return true;
        },
      );
    });
});

test('supportRequest bounds UTF-8 response size and rejects malformed JSON', async (t) => {
  fakeFetch(
    t,
    async () => new Response(JSON.stringify({ text: '😀'.repeat(5000) })),
  );
  await assert.rejects(
    supportRequest('preview', {}, new AbortController().signal, 100),
    /^Error: invalid$/,
  );
  t.mock.restoreAll();
  fakeFetch(t, async () => new Response('{not JSON'));
  await assert.rejects(
    supportRequest('preview', {}, new AbortController().signal, 100),
    SyntaxError,
  );
});

test('supportRequest times out stalled headers, aborts transport and never retries automatically', async (t) => {
  let count = 0,
    signal;
  fakeFetch(t, (_url, options) => {
    count++;
    signal = options.signal;
    return new Promise(() => {});
  });
  await assert.rejects(
    supportRequest('send', {}, new AbortController().signal, 15),
    /^Error: unconfirmed$/,
  );
  assert.equal(signal.aborted, true);
  assert.equal(count, 1);
});

test('supportRequest shares its deadline with a stalled response body', async (t) => {
  let signal,
    reads = 0;
  fakeFetch(t, async (_url, options) => {
    signal = options.signal;
    return {
      ok: true,
      text: () => {
        reads++;
        return new Promise(() => {});
      },
    };
  });
  await assert.rejects(
    supportRequest('preview', {}, new AbortController().signal, 15),
    /^Error: unconfirmed$/,
  );
  assert.equal(signal.aborted, true);
  assert.equal(reads, 1);
});

test('supportRequest forwards caller abort and removes its listener after completion', async (t) => {
  const caller = new AbortController();
  let signal;
  const removed = t.mock.method(caller.signal, 'removeEventListener');
  fakeFetch(t, (_url, options) => {
    signal = options.signal;
    return new Promise((resolve, reject) =>
      signal.addEventListener(
        'abort',
        () => reject(new DOMException('Cancelled', 'AbortError')),
        { once: true },
      ),
    );
  });
  const request = supportRequest('preview', {}, caller.signal, 500);
  caller.abort();
  await assert.rejects(request, { name: 'AbortError' });
  assert.equal(signal.aborted, true);
  assert.equal(removed.mock.calls.length, 1);
  assert.equal(removed.mock.calls[0].arguments[0], 'abort');
});

test('supportRequest begins aborted when the caller is already aborted', async (t) => {
  const caller = new AbortController();
  caller.abort();
  fakeFetch(t, async (_url, options) => {
    assert.equal(options.signal.aborted, true);
    throw new DOMException('Cancelled', 'AbortError');
  });
  await assert.rejects(supportRequest('preview', {}, caller.signal, 100), {
    name: 'AbortError',
  });
});

test('explicit retry after timeout retains the supplied ID and token with no hidden retry', async (t) => {
  const body = {
    reportId: preview().report.id,
    reviewToken: preview().reviewToken,
    confirmed: true,
  };
  const sent = [];
  fakeFetch(t, async (_url, options) => {
    sent.push(options.body);
    if (sent.length === 1) return new Promise(() => {});
    return new Response(
      JSON.stringify({ status: 'sent', reportId: body.reportId }),
    );
  });
  await assert.rejects(
    supportRequest('send', body, new AbortController().signal, 15),
    /^Error: unconfirmed$/,
  );
  assert.equal(sent.length, 1);
  await supportRequest('send', body, new AbortController().signal, 100);
  assert.equal(sent.length, 2);
  assert.equal(sent[0], sent[1]);
});

test('issue detection only changes local state, validates categories, and suppresses self-reporting', async (t) => {
  const { api, saved } = await store(t);
  let requests = 0;
  fakeFetch(t, async () => {
    requests++;
    throw Error('unexpected network');
  });
  for (const [category, area] of [
    ['manual', 'help'],
    ['raw-error', 'overview'],
    ['ui', 'https://private.example'],
  ])
    api.recordSupportIssue(category, area);
  assert.equal(api.getSupportIssues().prompt, null);
  assert.equal(saved.size, 0);
  api.setSupportReporting(true);
  api.recordSupportIssue('ui', 'overview');
  assert.equal(api.getSupportIssues().prompt, null);
  api.setSupportReporting(false);
  api.recordSupportIssue('ui', 'overview');
  assert.deepEqual(api.getSupportIssues().prompt, {
    category: 'ui',
    context: 'overview',
  });
  assert.equal(requests, 0);
});

test('one prompt wins across multiple capture paths and only safe keys/timestamps persist', async (t) => {
  const { api, saved } = await store(t);
  let notifications = 0;
  const unsubscribe = api.subscribeSupportIssues(() => notifications++);
  api.recordSupportIssue('ui', 'overview');
  api.recordSupportIssue('ui', 'overview');
  api.recordSupportIssue('action', 'models');
  assert.equal(notifications, 1);
  assert.deepEqual(api.getSupportIssues().prompt, {
    category: 'ui',
    context: 'overview',
  });
  assert.deepEqual(JSON.parse(saved.get(STORAGE)), {
    'ui:overview': NOW + 1800000,
  });
  unsubscribe();
  api.openSupportReport();
  assert.equal(notifications, 1);
});

test('automatic cooldown lasts thirty minutes without blocking manual reports', async (t) => {
  const { api, advance } = await store(t);
  api.recordSupportIssue('ui', 'overview');
  api.openSupportReport('ui', 'overview');
  assert.deepEqual(api.getSupportIssues().request, {
    category: 'ui',
    context: 'overview',
    sequence: 1,
  });
  advance(1800000 - 1);
  api.recordSupportIssue('ui', 'overview');
  assert.equal(api.getSupportIssues().prompt, null);
  api.openSupportReport('manual', 'help');
  assert.equal(api.getSupportIssues().request.sequence, 2);
  advance(1);
  api.recordSupportIssue('ui', 'overview');
  assert.deepEqual(api.getSupportIssues().prompt, {
    category: 'ui',
    context: 'overview',
  });
});

test('dismissal snoozes a prompt for one day across store reloads', async (t) => {
  const { api, reload, advance } = await store(t);
  api.recordSupportIssue('connection', 'network');
  api.dismissSupportIssue();
  const fresh = await reload();
  fresh.recordSupportIssue('connection', 'network');
  assert.equal(fresh.getSupportIssues().prompt, null);
  advance(DAY - 1);
  fresh.recordSupportIssue('connection', 'network');
  assert.equal(fresh.getSupportIssues().prompt, null);
  advance(1);
  fresh.recordSupportIssue('connection', 'network');
  assert.deepEqual(fresh.getSupportIssues().prompt, {
    category: 'connection',
    context: 'network',
  });
});

test('a different prompt after reload preserves previous persisted snoozes', async (t) => {
  const { api, reload, saved } = await store(t);
  api.recordSupportIssue('ui', 'overview');
  api.dismissSupportIssue();
  const fresh = await reload();
  fresh.recordSupportIssue('connection', 'network');
  fresh.dismissSupportIssue();
  assert.equal(JSON.parse(saved.get(STORAGE))['ui:overview'], NOW + DAY);
  const again = await reload();
  again.recordSupportIssue('ui', 'overview');
  assert.equal(again.getSupportIssues().prompt, null);
});

test('persisting a prompt retains only allowlisted coarse keys and bounded numeric expiries', async (t) => {
  const stored = {
    'ui:overview': NOW + DAY,
    'private:raw': NOW + DAY,
    'connection:https://private.example': NOW + DAY,
    'model:models': 'raw stack',
    'setup:setup': NOW + DAY + 1,
    'action:models': NOW - 1,
  };
  const { api, saved } = await store(t, { [STORAGE]: JSON.stringify(stored) });
  api.recordSupportIssue('connection', 'network');
  const persisted = JSON.parse(saved.get(STORAGE));
  for (const [key, value] of Object.entries(persisted)) {
    const [category, context] = key.split(':');
    assert.ok(api.supportCategories.includes(category));
    assert.ok(api.supportContexts.includes(context));
    assert.equal(typeof value, 'number');
    assert.ok(value > NOW && value <= NOW + DAY);
  }
  assert.equal(JSON.stringify(persisted).includes('private'), false);
  assert.equal(JSON.stringify(persisted).includes('raw stack'), false);
});

test('malformed and implausibly distant stored preferences cannot suppress reporting forever', async (t) => {
  const { api, saved } = await store(t, { [STORAGE]: '{broken json' });
  api.recordSupportIssue('ui', 'overview');
  assert.equal(api.getSupportIssues().prompt.category, 'ui');
  api.openSupportReport();
  saved.set(
    STORAGE,
    JSON.stringify({
      'connection:network': NOW + DAY + 1,
      'model:models': 'private',
    }),
  );
  api.recordSupportIssue('connection', 'network');
  assert.equal(api.getSupportIssues().prompt.category, 'connection');
});

test('unavailable browser storage does not block local prompting or manual reports', async (t) => {
  const { api } = await store(t);
  t.mock.method(localStorage, 'getItem', () => {
    throw Error('storage blocked');
  });
  t.mock.method(localStorage, 'setItem', () => {
    throw Error('storage blocked');
  });
  api.recordSupportIssue('ui', 'overview');
  assert.equal(api.getSupportIssues().prompt.category, 'ui');
  api.dismissSupportIssue();
  api.openSupportReport();
  assert.equal(api.getSupportIssues().request.category, 'manual');
});

test('connection conditions require at least sixty seconds of continuous failure', async (t) => {
  const { api, advance } = await store(t);
  api.observeSupportCondition('connection', 'earnings', true);
  advance(59999);
  api.observeSupportCondition('connection', 'earnings', true);
  assert.equal(api.getSupportIssues().prompt, null);
  advance(1);
  api.observeSupportCondition('connection', 'earnings', true);
  assert.deepEqual(api.getSupportIssues().prompt, {
    category: 'connection',
    context: 'earnings',
  });
});

test('a success resets the continuous-failure timer and conditions remain independent', async (t) => {
  const { api, advance } = await store(t);
  api.observeSupportCondition('connection', 'earnings', true);
  advance(40000);
  api.observeSupportCondition('connection', 'earnings', false);
  api.observeSupportCondition('connection', 'network', true);
  advance(20000);
  api.observeSupportCondition('connection', 'earnings', true);
  assert.equal(api.getSupportIssues().prompt, null);
  advance(39999);
  api.observeSupportCondition('connection', 'earnings', true);
  api.observeSupportCondition('connection', 'network', true);
  assert.equal(api.getSupportIssues().prompt, null);
  advance(1);
  api.observeSupportCondition('connection', 'network', true);
  assert.deepEqual(api.getSupportIssues().prompt, {
    category: 'connection',
    context: 'network',
  });
});

test('only fresh failed or recovered model results prompt; raw result details are discarded', async (t) => {
  const { api, advance, saved } = await store(t);
  for (const value of [
    null,
    {},
    { at: '123', outcome: 'failed' },
    { at: Infinity, outcome: 'failed' },
    { at: NOW / 1000 - 1, outcome: 'failed' },
    { at: NOW / 1000 + 301, outcome: 'failed' },
    { at: NOW / 1000, outcome: 'switched' },
    { at: NOW / 1000, outcome: 'deferred' },
    { at: NOW / 1000, outcome: 'private-error' },
  ])
    api.observeModelResult(value);
  assert.equal(api.getSupportIssues().prompt, null);
  advance(1000);
  api.observeModelResult({
    at: NOW / 1000 + 1,
    outcome: 'failed',
    error: 'secret stack and account',
    model: 'private model',
    path: '/Users/private',
  });
  assert.deepEqual(api.getSupportIssues().prompt, {
    category: 'model',
    context: 'models',
  });
  assert.equal(JSON.stringify([...saved]).includes('secret'), false);
  assert.equal(
    JSON.stringify(api.getSupportIssues()).includes('private'),
    false,
  );
});

test('same model result stays deduplicated after cooldown while a fresh recovery can prompt', async (t) => {
  const { api, advance } = await store(t);
  const result = { at: NOW / 1000, outcome: 'failed' };
  api.observeModelResult(result);
  api.openSupportReport('model', 'models');
  advance(1800000);
  api.observeModelResult(result);
  assert.equal(api.getSupportIssues().prompt, null);
  api.observeModelResult({ at: Date.now() / 1000, outcome: 'recovered' });
  assert.equal(api.getSupportIssues().prompt.category, 'model');
});

test('unknown navigation context stays coarse and SSR state remains empty', async (t) => {
  const { api } = await store(t);
  for (const [input, expected] of [
    ['credits', 'earnings'],
    ['optimizer-tools', 'models'],
    ['processes', 'hardware'],
    ['fleet', 'network'],
    ['access', 'phone'],
    ['support', 'help'],
    ['setup', 'setup'],
    ['https://private.example/path?secret=yes', 'unknown'],
  ])
    assert.equal(api.supportContext(input), expected);
  api.setSupportContext('https://private.example/path?secret=yes');
  api.recordSupportIssue('ui');
  assert.deepEqual(api.getSupportIssues().prompt, {
    category: 'ui',
    context: 'unknown',
  });
  assert.deepEqual(api.getServerSupportIssues(), {
    prompt: null,
    request: null,
  });
});

test('one-tap report previews then sends the exact frozen report and returns its ID', async (t) => {
  t.mock.method(Date, 'now', () => NOW);
  const input = {
    category: 'model',
    context: 'models',
    description: 'switch failed',
    contact: '',
  };
  const calls = [];
  fakeFetch(t, async (url, options) => {
    calls.push([
      url,
      JSON.parse(options.body),
      options.headers['X-Bloom-Action'],
    ]);
    const body = url.endsWith('/preview')
      ? { report: preview(input).report, reviewToken: 'a'.repeat(64) }
      : { status: 'sent', reportId: preview(input).report.id };
    return new Response(JSON.stringify(body), { status: 200 });
  });
  assert.equal(
    await quickSupportReport(input, new AbortController().signal),
    preview(input).report.id,
  );
  assert.deepEqual(
    calls.map((c) => c[0]),
    ['/api/support/preview', '/api/support/send'],
  );
  assert.deepEqual(calls[0][1], input);
  assert.deepEqual(calls[1][1], {
    reportId: preview(input).report.id,
    reviewToken: 'a'.repeat(64),
    confirmed: true,
  });
  assert.ok(calls.every((c) => c[2] === 'support'));
});
test('automatic reports tell the Mac app so it can apply its shared limit', async (t) => {
  t.mock.method(Date, 'now', () => NOW);
  const input = {
    category: 'model',
    context: 'models',
    description: '',
    contact: '',
  };
  const bodies = [];
  fakeFetch(t, async (url, options) => {
    bodies.push(JSON.parse(options.body));
    const body = url.endsWith('/preview')
      ? { report: preview(input).report, reviewToken: 'a'.repeat(64) }
      : { status: 'sent', reportId: preview(input).report.id };
    return new Response(JSON.stringify(body), { status: 200 });
  });
  await quickSupportReport(input, new AbortController().signal, true);
  assert.deepEqual(bodies[0], { ...input, automatic: true });
  assert.equal(bodies.length, 2);
});
test('one-tap report never sends when the preview does not match the tapped issue', async (t) => {
  t.mock.method(Date, 'now', () => NOW);
  const sent = [];
  fakeFetch(t, async (url) => {
    sent.push(url);
    return new Response(
      JSON.stringify({ report: preview().report, reviewToken: 'a'.repeat(64) }),
      { status: 200 },
    );
  });
  await assert.rejects(
    quickSupportReport(
      { category: 'model', context: 'models', description: '', contact: '' },
      new AbortController().signal,
    ),
    SupportRequestError,
  );
  assert.deepEqual(sent, ['/api/support/preview']);
});
test('auto-send setting reads only an explicit true and writes through the support route', async (t) => {
  const bodies = [];
  fakeFetch(t, async (url, options) => {
    if (!options?.method)
      return new Response(
        JSON.stringify(url.endsWith('/auto') ? { autoSend: 'true' } : {}),
        { status: 200 },
      );
    bodies.push(JSON.parse(options.body));
    return new Response(JSON.stringify({ autoSend: true }), { status: 200 });
  });
  assert.equal(await readAutoSend(new AbortController().signal), false);
  assert.equal(await writeAutoSend(true, new AbortController().signal), true);
  assert.deepEqual(bodies, [{ autoSend: true }]);
});

test('an automatic report mutes the same problem for six hours', async (t) => {
  const { api, saved } = await store(t);
  api.recordSupportIssue('connection', 'models');
  api.clearAutoSentPrompt();
  assert.equal(api.getSupportIssues().prompt, null);
  assert.equal(
    JSON.parse(saved.get(STORAGE))['connection:models'],
    NOW + 6 * 3600000,
  );
  api.recordSupportIssue('connection', 'models');
  assert.equal(api.getSupportIssues().prompt, null);
});
