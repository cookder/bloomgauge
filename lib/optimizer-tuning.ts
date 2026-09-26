// Optimizer tuning: numeric ranges for each control (mirrors native/demand_optimizer.py
// RANGES) and the passive-to-aggressive presets the style slider applies.
import type { DemandRules } from '@/components/dashboard/demand-auto';

export type TunedKey =
  | 'idleEscapeMinutes'
  | 'trialMinutes'
  | 'minRunMinutes'
  | 'confirmationMinutes'
  | 'trialCooldownMinutes'
  | 'improvementPercent'
  | 'minimumNetUsd'
  | 'planningMinutes'
  | 'memoryHeadroomGB'
  | 'maxSwitchesPerDay'
  | 'maxDowntimeMinutes'
  | 'protectUsdPerHour'
  | 'learningMinutesPerDay';

export const tuningRanges: Record<
  TunedKey,
  { min: number; max: number; step: number }
> = {
  idleEscapeMinutes: { min: 10, max: 120, step: 5 },
  trialMinutes: { min: 10, max: 60, step: 5 },
  minRunMinutes: { min: 15, max: 480, step: 15 },
  confirmationMinutes: { min: 1, max: 60, step: 1 },
  trialCooldownMinutes: { min: 10, max: 480, step: 5 },
  improvementPercent: { min: 5, max: 100, step: 5 },
  minimumNetUsd: { min: 0.005, max: 0.2, step: 0.005 },
  planningMinutes: { min: 30, max: 240, step: 15 },
  memoryHeadroomGB: { min: 1, max: 8, step: 0.5 },
  maxSwitchesPerDay: { min: 1, max: 48, step: 1 },
  maxDowntimeMinutes: { min: 5, max: 120, step: 5 },
  protectUsdPerHour: { min: 0.05, max: 1, step: 0.01 },
  learningMinutesPerDay: { min: 0, max: 480, step: 15 },
};

/** Clamp to the range and snap to its step, avoiding float drift (0.1 + 0.2). */
export function snap(key: TunedKey, value: number) {
  const { min, max, step } = tuningRanges[key];
  if (!Number.isFinite(value)) return min;
  const steps = Math.round((Math.min(max, Math.max(min, value)) - min) / step);
  return Number((min + steps * step).toFixed(3));
}

// Memory headroom, GPT-OSS fallback, the protect level and learning time are
// separate choices, so the style slider leaves them alone. "Balanced" equals the defaults.
type StyleValues = Omit<
  Pick<DemandRules, TunedKey | 'baselineLearningEnabled'>,
  'memoryHeadroomGB' | 'protectUsdPerHour' | 'learningMinutesPerDay'
>;
export const optimizerStyles: {
  id: string;
  label: string;
  summary: string;
  values: StyleValues;
}[] = [
  {
    id: 'very-passive',
    label: 'Very passive',
    summary:
      'Switches rarely and only for large, confirmed gains. No learning trials.',
    values: {
      idleEscapeMinutes: 60,
      trialMinutes: 30,
      minRunMinutes: 120,
      confirmationMinutes: 15,
      trialCooldownMinutes: 120,
      improvementPercent: 50,
      minimumNetUsd: 0.05,
      planningMinutes: 120,
      maxSwitchesPerDay: 4,
      maxDowntimeMinutes: 15,
      baselineLearningEnabled: 0,
    },
  },
  {
    id: 'passive',
    label: 'Passive',
    summary: 'Waits longer and needs clearer gains before switching.',
    values: {
      idleEscapeMinutes: 45,
      trialMinutes: 20,
      minRunMinutes: 60,
      confirmationMinutes: 10,
      trialCooldownMinutes: 60,
      improvementPercent: 30,
      minimumNetUsd: 0.03,
      planningMinutes: 120,
      maxSwitchesPerDay: 8,
      maxDowntimeMinutes: 20,
      baselineLearningEnabled: 1,
    },
  },
  {
    id: 'balanced',
    label: 'Balanced',
    summary:
      'The default. Moves on from idle models after 20 minutes and needs a 20% gain.',
    values: {
      idleEscapeMinutes: 20,
      trialMinutes: 20,
      minRunMinutes: 30,
      confirmationMinutes: 5,
      trialCooldownMinutes: 30,
      improvementPercent: 20,
      minimumNetUsd: 0.02,
      planningMinutes: 60,
      maxSwitchesPerDay: 12,
      maxDowntimeMinutes: 30,
      baselineLearningEnabled: 1,
    },
  },
  {
    id: 'aggressive',
    label: 'Aggressive',
    summary: 'Tries other models sooner and switches for smaller gains.',
    values: {
      idleEscapeMinutes: 15,
      trialMinutes: 15,
      minRunMinutes: 30,
      confirmationMinutes: 3,
      trialCooldownMinutes: 20,
      improvementPercent: 15,
      minimumNetUsd: 0.01,
      planningMinutes: 60,
      maxSwitchesPerDay: 18,
      maxDowntimeMinutes: 45,
      baselineLearningEnabled: 1,
    },
  },
  {
    id: 'very-aggressive',
    label: 'Very aggressive',
    summary:
      'Chases demand quickly. More switches and more time loading models.',
    values: {
      idleEscapeMinutes: 10,
      trialMinutes: 15,
      minRunMinutes: 15,
      confirmationMinutes: 2,
      trialCooldownMinutes: 15,
      improvementPercent: 10,
      minimumNetUsd: 0.005,
      planningMinutes: 30,
      maxSwitchesPerDay: 24,
      maxDowntimeMinutes: 60,
      baselineLearningEnabled: 1,
    },
  },
];

/** Index of the style the rules match exactly, or null when they have been customized. */
export function styleIndex(rules: DemandRules) {
  const index = optimizerStyles.findIndex((style) =>
    (Object.keys(style.values) as (keyof StyleValues)[]).every(
      (key) => Math.abs(rules[key] - style.values[key]) < 1e-9,
    ),
  );
  return index < 0 ? null : index;
}

export function applyStyle(rules: DemandRules, index: number): DemandRules {
  return { ...rules, ...optimizerStyles[index].values };
}
