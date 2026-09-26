import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyStyle,
  optimizerStyles,
  snap,
  styleIndex,
  tuningRanges,
} from '../lib/optimizer-tuning.ts';

const defaults = {
  minRunMinutes: 30,
  confirmationMinutes: 5,
  improvementPercent: 20,
  planningMinutes: 60,
  minimumNetUsd: 0.02,
  maxSwitchesPerDay: 12,
  maxDowntimeMinutes: 30,
  memoryHeadroomGB: 1,
  idleEscapeMinutes: 20,
  trialMinutes: 20,
  trialCooldownMinutes: 30,
  fallbackEnabled: 1,
  targetUsdPerHour: 0.12,
  baselineLearningEnabled: 1,
  protectUsdPerHour: 0.2,
  learningMinutesPerDay: 60,
};

test('every style is valid for the backend and Balanced equals the defaults', () => {
  assert.equal(optimizerStyles[styleIndex(defaults)].id, 'balanced');
  for (const style of optimizerStyles) {
    const rules = applyStyle(defaults, optimizerStyles.indexOf(style));
    assert.ok(rules.confirmationMinutes <= rules.minRunMinutes, style.id);
    for (const [key, range] of Object.entries(tuningRanges)) {
      assert.equal(
        snap(key, rules[key]),
        rules[key],
        `${style.id} ${key} is on a step within range`,
      );
    }
    assert.equal(styleIndex(rules), optimizerStyles.indexOf(style));
    assert.equal(
      rules.memoryHeadroomGB,
      defaults.memoryHeadroomGB,
      'styles never change memory headroom',
    );
  }
});

test('styles move steadily from passive to aggressive', () => {
  const values = optimizerStyles.map((s) => s.values);
  for (let i = 1; i < values.length; i++) {
    assert.ok(values[i].idleEscapeMinutes <= values[i - 1].idleEscapeMinutes);
    assert.ok(values[i].improvementPercent <= values[i - 1].improvementPercent);
    assert.ok(values[i].maxSwitchesPerDay >= values[i - 1].maxSwitchesPerDay);
  }
});

test('a changed setting reads as custom, and typed values snap into range', () => {
  assert.equal(styleIndex({ ...defaults, idleEscapeMinutes: 35 }), null);
  assert.equal(snap('idleEscapeMinutes', 37), 35);
  assert.equal(snap('idleEscapeMinutes', 3), 10);
  assert.equal(snap('minimumNetUsd', 0.0149), 0.015);
  assert.equal(snap('memoryHeadroomGB', 0), 1);
  assert.equal(snap('maxSwitchesPerDay', Number.NaN), 1);
});

test('UI ranges match the backend validation', async () => {
  const { readFile } = await import('node:fs/promises');
  const source = await readFile(
    new URL('./demand_optimizer.py', import.meta.url),
    'utf8',
  );
  const block = source.slice(
    source.indexOf('RANGES = {'),
    source.indexOf('CHOICES = {'),
  );
  const backend = Object.fromEntries(
    [...block.matchAll(/'(\w+)': \(([\d.]+), ([\d.]+), ([\d.]+)\)/g)].map(
      ([, key, min, max, step]) => [
        key,
        { min: Number(min), max: Number(max), step: Number(step) },
      ],
    ),
  );
  assert.deepEqual(backend, tuningRanges);
});
