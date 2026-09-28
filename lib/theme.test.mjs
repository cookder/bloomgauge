import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {
  THEME_CHROME,
  THEME_STORAGE_KEY,
  nextThemePreference,
  parseThemePreference,
  readThemePreference,
  resolveTheme,
  writeThemePreference,
} from './theme.ts';
import {
  contrast,
  convert,
  lightColour,
  paletteFile,
  rawColour,
  renderPalette,
  themedFiles,
  usedTokens,
} from '../tools/theme-palette.mjs';

const read = (relative) =>
  fs.readFileSync(new URL(`../${relative}`, import.meta.url), 'utf8');
const memoryStorage = (initial = {}) => {
  const values = { ...initial };
  return {
    values,
    getItem: (key) => (key in values ? values[key] : null),
    setItem: (key, value) => {
      values[key] = String(value);
    },
  };
};
const throwing = {
  getItem() {
    throw new Error('SecurityError');
  },
  setItem() {
    throw new Error('QuotaExceededError');
  },
};

test('System follows the OS; Light and Dark ignore it', () => {
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
  assert.equal(resolveTheme('system', null), 'dark');
  for (const system of [true, false, null]) {
    assert.equal(resolveTheme('light', system), 'light');
    assert.equal(resolveTheme('dark', system), 'dark');
  }
});

test('unknown or missing values mean System', () => {
  for (const value of [null, undefined, '', 'Light', 'auto', 1, {}])
    assert.equal(parseThemePreference(value), 'system');
  for (const value of ['system', 'light', 'dark'])
    assert.equal(parseThemePreference(value), value);
});

test('the choice round-trips through storage', () => {
  const storage = memoryStorage();
  assert.equal(readThemePreference(storage), 'system');
  assert.equal(writeThemePreference(storage, 'light'), true);
  assert.equal(storage.values[THEME_STORAGE_KEY], 'light');
  assert.equal(readThemePreference(storage), 'light');
  assert.equal(
    readThemePreference(memoryStorage({ [THEME_STORAGE_KEY]: 'sepia' })),
    'system',
  );
});

test('blocked or missing storage falls back without throwing', () => {
  assert.equal(readThemePreference(throwing), 'system');
  assert.equal(readThemePreference(null), 'system');
  assert.equal(readThemePreference(undefined), 'system');
  assert.equal(writeThemePreference(throwing, 'dark'), false);
  assert.equal(writeThemePreference(null, 'dark'), false);
});

test('the header button cycles System, Light, Dark', () => {
  assert.equal(nextThemePreference('system'), 'light');
  assert.equal(nextThemePreference('light'), 'dark');
  assert.equal(nextThemePreference('dark'), 'system');
});

// Runs public/theme-boot.js against a fake page, as the browser would before
// the first paint.
function boot({
  saved,
  systemDark,
  storageThrows = false,
  noMatchMedia = false,
}) {
  const attributes = {};
  const classes = new Set(['dark']);
  const meta = {
    content: '#0a0d12',
    setAttribute: (_, v) => (meta.content = v),
  };
  const window = {
    localStorage: storageThrows
      ? throwing
      : memoryStorage(saved ? { [THEME_STORAGE_KEY]: saved } : {}),
    matchMedia: noMatchMedia
      ? undefined
      : (query) => ({
          matches: query === '(prefers-color-scheme: dark)' && systemDark,
        }),
  };
  const document = {
    documentElement: {
      setAttribute: (name, value) => (attributes[name] = value),
      classList: {
        add: (name) => classes.add(name),
        remove: (name) => classes.delete(name),
      },
    },
    querySelector: () => meta,
  };
  vm.runInNewContext(read('public/theme-boot.js'), { window, document });
  return {
    theme: attributes['data-theme'],
    preference: attributes['data-theme-preference'],
    darkClass: classes.has('dark'),
    chrome: meta.content,
  };
}

test('the boot script agrees with lib/theme.ts', () => {
  for (const saved of [undefined, 'system', 'light', 'dark', 'junk'])
    for (const systemDark of [true, false]) {
      const preference = parseThemePreference(saved);
      const expected = resolveTheme(preference, systemDark);
      assert.deepEqual(boot({ saved, systemDark }), {
        theme: expected,
        preference,
        darkClass: expected === 'dark',
        chrome: THEME_CHROME[expected],
      });
    }
  assert.equal(boot({ storageThrows: true, systemDark: false }).theme, 'light');
  assert.equal(boot({ saved: 'system', noMatchMedia: true }).theme, 'dark');
  assert.equal(boot({ saved: 'light', noMatchMedia: true }).theme, 'light');
});

test('index.html loads the boot script before the app', () => {
  const html = read('index.html');
  const bootAt = html.indexOf('<script src="/theme-boot.js"></script>');
  assert.ok(bootAt > 0 && bootAt < html.indexOf('</head>'));
  assert.ok(bootAt < html.indexOf('/local-entry.tsx'));
});

test('themed files use colour tokens, not raw hex or rgb colours', () => {
  for (const file of themedFiles) {
    const lines = read(file).split('\n');
    const raw = lines
      .map((line, index) => [index + 1, line])
      .filter(([, line]) => rawColour.test(line));
    assert.deepEqual(
      raw.map(([n, line]) => `${file}:${n}: ${line.trim()}`),
      [],
      'run node tools/theme-palette.mjs',
    );
  }
});

test('every colour token has a dark and a light value', () => {
  const tokens = themedFiles.flatMap((file) => usedTokens(read(file)));
  assert.ok(tokens.length > 100);
  assert.equal(
    read(paletteFile),
    renderPalette(tokens),
    'app/theme-palette.css is stale: run node tools/theme-palette.mjs',
  );
  assert.match(read('app/globals.css'), /@import '\.\/theme-palette\.css';/);
  assert.match(
    read('app/globals.css'),
    /:root\[data-theme='light'\] \{\s*color-scheme: light;/,
  );
});

test('literals convert to tokens named after their dark value', () => {
  assert.equal(convert("fill='#82EFB5'"), "fill='var(--c-82efb5)'");
  assert.equal(convert('#fff #0007'), 'var(--c-ffffff) var(--c-00000077)');
  assert.equal(convert('rgba(130, 239, 181, 0.5)'), 'var(--c-82efb580)');
  assert.equal(convert('url(#grad) %23a3b2be'), 'url(#grad) %23a3b2be');
});

test('light text and status colours stay legible on light surfaces', () => {
  const page = lightColour('0a0d12');
  const card = lightColour('11161e');
  assert.equal(card, 'ffffff');
  for (const [dark, minimum] of [
    ['eef2f8', 7], // body text
    ['93a0b4', 4.5], // muted text
    ['8997aa', 4.5], // chart axis labels
    ['82efb5', 4.5], // green: healthy, earnings
    ['f3c57e', 4.5], // amber: warm, waiting
    ['ff8d88', 4.5], // red: hot, errors
    ['87b9ff', 4.5], // blue: cool
    ['a995ff', 4.5], // violet chart line
  ]) {
    const light = lightColour(dark);
    assert.ok(
      contrast(light, card) >= minimum &&
        contrast(light, page) >= minimum - 0.4,
      `#${dark} -> #${light}: ${contrast(light, card).toFixed(2)}`,
    );
  }
  // Gridlines stay visible but quiet.
  const grid = contrast(lightColour('27303c'), card);
  assert.ok(grid > 1.2 && grid < 2, `grid ${grid}`);
});
