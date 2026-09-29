#!/usr/bin/env node
// Colour tokens for the light and dark themes. Owner tool; never bundled.
//
// The UI was designed dark-first, with hundreds of literal colours. Each one is
// now a token named after its dark value (`#35584a` -> `var(--c-35584a)`), and
// app/theme-palette.css defines both values: the dark one unchanged, and a light
// one derived here in OKLCH (surfaces flip to near-white, text flips to dark,
// accents keep their hue at a darker, legible lightness), with hand-picked
// overrides for the core surfaces and status colours.
//
//     node tools/theme-palette.mjs         # convert new literals, rewrite the palette
//     node tools/theme-palette.mjs --check # exit 1 if anything would change
//
// To add a colour, write it as `var(--c-<hex>)` (or as a literal, then run this).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// Files whose colours must all be tokens (lib/theme.test.mjs checks this).
export const themedFiles = [
  'app/globals.css',
  'app/page.tsx',
  'components/dashboard/contribution-chart.tsx',
  'components/dashboard/demand-baselines.tsx',
  'components/dashboard/earnings-outlook.tsx',
  'components/dashboard/earnings-pulse.tsx',
  'components/dashboard/forecast.tsx',
  'components/dashboard/model-demand.tsx',
  'components/dashboard/model-history.tsx',
  'components/dashboard/model-research.tsx',
  'components/dashboard/network-contributions.tsx',
  'components/dashboard/operating-history.tsx',
  'components/dashboard/optimizer-tab.tsx',
  'components/dashboard/rate-trend.tsx',
  'components/dashboard/shared.tsx',
  'components/dashboard/traffic-pulse.tsx',
  'components/dashboard/widgets.tsx',
  'components/dashboard/workload.tsx',
  'lib/earnings-pulse.ts',
  'lib/model-earnings.ts',
];
export const paletteFile = 'app/theme-palette.css';

// Light values chosen by hand: page and card surfaces, the brand accent and the
// status colours (green / amber / red / blue / violet), checked for contrast on
// white and on the light page background.
export const lightOverrides = {
  '0a0d12': 'f3f5f8', // page background
  '11161e': 'ffffff', // card
  '1c2430': 'e9edf2', // muted surface
  '26303d': 'd5dce5', // border
  '93a0b4': '5a6678', // muted text
  eef2f8: '141a22', // text
  '82efb5': '0b7d48', // brand green / healthy
  '83e6b6': '0b7d48',
  '99efc7': '0a7a45',
  f3c57e: 'a05f00', // amber / warm / waiting
  ffd079: '9a6100',
  ff8d88: 'c62f2a', // red / hot
  '87b9ff': '1f63c9', // blue / cool
  '91bcff': '1f63c9',
  a995ff: '6a4fd8', // violet
  b49cff: '6a4fd8',
  '07130c': 'ffffff', // text on the green accent
};

// -- colour maths (OKLab, https://bottosson.github.io/posts/oklab/) --
const toLinear = (c) =>
  c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
const fromLinear = (c) =>
  c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055;
function rgbToOklch([r, g, b]) {
  [r, g, b] = [r, g, b].map((v) => toLinear(v / 255));
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  return [L, Math.hypot(A, B), Math.atan2(B, A)];
}
function oklchToLinear([L, C, h]) {
  const A = C * Math.cos(h),
    B = C * Math.sin(h);
  const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3;
  const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3;
  const s = (L - 0.0894841775 * A - 1.291485548 * B) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.7076956424 * s,
  ];
}
const inGamut = (rgb) => rgb.every((v) => v >= -1e-4 && v <= 1 + 1e-4);
function oklchToRgb([L, C, h]) {
  let lo = 0,
    hi = C;
  if (!inGamut(oklchToLinear([L, C, h]))) {
    for (let i = 0; i < 24; i++) {
      const mid = (lo + hi) / 2;
      if (inGamut(oklchToLinear([L, mid, h]))) lo = mid;
      else hi = mid;
    }
    C = lo;
  }
  return oklchToLinear([L, C, h]).map((v) =>
    Math.round(Math.min(1, Math.max(0, fromLinear(Math.max(0, v)))) * 255),
  );
}
const hex2 = (n) => n.toString(16).padStart(2, '0');
export function parseHex(hex) {
  hex = hex.replace('#', '').toLowerCase();
  if (hex.length <= 4) hex = [...hex].map((c) => c + c).join('');
  const rgb = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
  const alpha = hex.length === 8 ? parseInt(hex.slice(6, 8), 16) : 255;
  return { rgb, alpha };
}
/** Relative luminance contrast ratio (WCAG) between two opaque hex colours. */
export function contrast(a, b) {
  const lum = (hex) => {
    const [r, g, bl] = parseHex(hex).rgb.map((v) => toLinear(v / 255));
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

/** The light-theme value for a dark-theme colour (6 or 8 hex digits). */
export function lightColour(token) {
  const base = token.slice(0, 6);
  const alphaHex = token.length === 8 ? token.slice(6) : '';
  if (lightOverrides[base]) return lightOverrides[base] + alphaHex;
  const { rgb, alpha } = parseHex(token);
  const [L, C, h] = rgbToOklch(rgb);
  // Shadows and dimming layers stay black, just lighter.
  if (alpha < 255 && L < 0.12)
    return '000000' + hex2(Math.max(1, Math.round(alpha * 0.4)));
  let light, chroma;
  if (C >= 0.06 && L >= 0.5) {
    // Accent: keep the hue, darken until it reads on white.
    light = 0.46 + (1 - L) * 0.3;
    chroma = C;
  } else if (L < 0.175) {
    // Page and inset wells: a quiet grey under white cards.
    light = 0.955 + (L - 0.1) * 0.2;
    chroma = Math.min(C, 0.012);
  } else {
    // Surfaces, borders and text: flip lightness.
    light = Math.min(0.995, Math.max(0.2, 0.995 - (L - 0.19) * 1.14));
    chroma = light > 0.85 ? Math.min(C * 0.6, 0.02) : C;
  }
  return oklchToRgb([light, chroma, h]).map(hex2).join('') + alphaHex;
}

// -- literal -> token conversion --
const literal =
  /#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b|rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)/g;
export const rawColour = new RegExp(literal.source);
function tokenFor(match, hex, r, g, b, a) {
  if (hex) {
    const { rgb, alpha } = parseHex(hex);
    return rgb.map(hex2).join('') + (alpha < 255 ? hex2(alpha) : '');
  }
  const alpha = a === undefined ? 255 : Math.round(Number(a) * 255);
  return (
    [r, g, b].map((v) => hex2(Number(v))).join('') +
    (alpha < 255 ? hex2(alpha) : '')
  );
}
export function convert(source) {
  return source.replace(literal, (...m) => `var(--c-${tokenFor(...m)})`);
}
export const usedTokens = (source) =>
  [...source.matchAll(/var\(--c-([0-9a-f]{6}(?:[0-9a-f]{2})?)\)/g)].map(
    (m) => m[1],
  );

export function renderPalette(tokens) {
  const sorted = [...new Set(tokens)].sort();
  const block = (value) =>
    sorted.map((t) => `  --c-${t}: #${value(t)};`).join('\n');
  return `/* Generated by tools/theme-palette.mjs; edit that file, not this one.
   Each token is named after its dark-theme value. The light values are derived
   in OKLCH, with hand-picked overrides for surfaces and status colours. */
:root {
${block((t) => t)}
}
:root[data-theme='light'] {
${block(lightColour)}
}
`;
}

function main() {
  const check = process.argv.includes('--check');
  const tokens = [];
  let changed = [];
  for (const file of themedFiles) {
    const full = path.join(root, file);
    const before = fs.readFileSync(full, 'utf8');
    const after = convert(before);
    tokens.push(...usedTokens(after));
    if (after !== before) {
      changed.push(file);
      if (!check) fs.writeFileSync(full, after);
    }
  }
  const palette = renderPalette(tokens);
  const paletteFull = path.join(root, paletteFile);
  const current = fs.existsSync(paletteFull)
    ? fs.readFileSync(paletteFull, 'utf8')
    : '';
  if (palette !== current) {
    changed.push(paletteFile);
    if (!check) fs.writeFileSync(paletteFull, palette);
  }
  console.log(
    changed.length
      ? `${check ? 'Would change' : 'Changed'}: ${changed.join(', ')}`
      : 'Palette up to date.',
  );
  if (check && changed.length) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href)
  main();
