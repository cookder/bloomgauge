'use client';
import { useState, type ReactNode } from 'react';
import { distinctLabels } from '@/lib/model-label';
import { Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Choice, money, shortModel } from './shared';
import {
  DataGatheringControl,
  DemandSettings,
  NumberSetting,
  type DataGathering,
  type DemandRules,
} from './demand-auto';
import {
  applyStyle,
  optimizerStyles,
  snap,
  styleIndex,
  tuningRanges,
} from '@/lib/optimizer-tuning';

const learningChoices = [0, 30, 60, 120, 180, 240];
const learningLabel = (minutes: number) =>
  minutes === 0
    ? 'Off'
    : minutes < 60
      ? `${minutes} min a day`
      : `${Number((minutes / 60).toFixed(2))} hour${minutes === 60 ? '' : 's'} a day`;

export type PlanModel = {
  id: string;
  name: string;
  available: boolean;
  reason?: string;
};

const sameSet = (a: string[], b: string[]) =>
  a.length === b.length && a.every((id) => b.includes(id));

/** Follow demand's plan, edited in place on the optimizer card. */
export function OptimizerPlan({
  on,
  models,
  currentModel,
  selected,
  onSelected,
  rules,
  onRules,
  savedSelected,
  savedRules,
  gathering,
  onGathering,
  onSave,
  onDiscard,
  editable,
  lockedReason,
  busy,
  children,
}: {
  on: boolean;
  models: PlanModel[];
  currentModel?: string;
  selected: string[];
  onSelected: (next: string[]) => void;
  rules: DemandRules;
  onRules: (next: DemandRules) => void;
  savedSelected: string[];
  savedRules?: DemandRules;
  gathering?: DataGathering;
  onGathering: (seconds: number) => void;
  onSave: () => void;
  onDiscard: () => void;
  editable: boolean;
  lockedReason: string;
  busy: boolean;
  children?: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const dirty =
    !sameSet(selected, savedSelected) ||
    (!!savedRules && JSON.stringify(rules) !== JSON.stringify(savedRules));
  const choices = [
    ...models,
    ...selected
      .filter((id) => !models.some((m) => m.id === id))
      .map((id) => ({
        id,
        name: id,
        available: false,
        reason: 'Not in the current catalog',
      })),
  ];
  const tooFew = selected.length < 2;
  const confirmTooLong = rules.confirmationMinutes > rules.minRunMinutes;
  const style = styleIndex(rules);
  const shownStyle = style ?? 2;
  const toggle = (id: string) =>
    onSelected(
      selected.includes(id)
        ? selected.filter((m) => m !== id)
        : [...selected, id],
    );
  if (!on && !editing && !dirty) {
    return (
      <div className="optimizer-plan-summary">
        <span>
          When on, Bloomkeeper chooses from{' '}
          <strong>{savedSelected.length || 'all available'} models</strong>,
          protects pace above{' '}
          <strong>{money(rules.protectUsdPerHour)} / hour</strong> and{' '}
          {rules.learningMinutesPerDay ? (
            <>
              learns for up to{' '}
              <strong>{learningLabel(rules.learningMinutesPerDay)}</strong>
            </>
          ) : (
            <>
              has learning <strong>off</strong>
            </>
          )}
          .
        </span>
        <button
          type="button"
          className="text-link"
          onClick={() => setEditing(true)}
        >
          Edit
        </button>
      </div>
    );
  }
  const labels = distinctLabels(
    choices.map((m) => m.id),
    shortModel,
  );
  return (
    <div className="optimizer-plan" aria-label="Optimizer plan">
      <p className="optimizer-plan-label">Models Bloomkeeper can use</p>
      <div
        className="optimizer-plan-models"
        role="group"
        aria-label="Models Bloomkeeper can use"
      >
        {choices.map((model) => {
          const label = labels(model.id);
          const chosen = selected.includes(model.id);
          const serving = on && model.id === currentModel;
          return (
            <button
              key={model.id}
              type="button"
              aria-pressed={chosen}
              title={
                serving
                  ? 'Serving now; stays selected while on'
                  : model.reason || undefined
              }
              disabled={!editable || serving || (!model.available && !chosen)}
              onClick={() => toggle(model.id)}
            >
              {chosen && <Check size={13} aria-hidden="true" />}
              {label}
              {serving && <small>serving</small>}
            </button>
          );
        })}
      </div>
      <div className="optimizer-plan-grid">
        <NumberSetting
          id="protect-level"
          label="Protect earnings above"
          unit="USD / hour"
          disabled={!editable}
          hint="While the current model pays at least this, Bloomkeeper won’t interrupt it to learn. Confident upgrades can still switch."
          value={rules.protectUsdPerHour}
          range={tuningRanges.protectUsdPerHour}
          onChange={(next) =>
            onRules({
              ...rules,
              protectUsdPerHour: snap('protectUsdPerHour', next),
            })
          }
        />
        <label>
          Learning time
          <Choice
            value={String(rules.learningMinutesPerDay)}
            disabled={!editable}
            label="Learning time per day"
            onChange={(next) =>
              onRules({ ...rules, learningMinutesPerDay: Number(next) })
            }
            options={[
              ...new Set([...learningChoices, rules.learningMinutesPerDay]),
            ]
              .sort((a, b) => a - b)
              .map((v) => ({ value: String(v), label: learningLabel(v) }))}
          />
          <small className="plan-hint">
            Time Bloomkeeper may spend measuring other models while pace is below your
            protect level, so it knows where to go when the current model fades.
          </small>
        </label>
        <DataGatheringControl
          compact
          value={gathering}
          onSet={onGathering}
          disabled={busy || !on}
        />
      </div>
      <div className="optimizer-style">
        <div className="optimizer-style-head">
          <label htmlFor="optimizer-style">How actively Bloomkeeper switches</label>
          <strong>
            {style === null ? 'Custom' : optimizerStyles[style].label}
          </strong>
        </div>
        <input
          id="optimizer-style"
          type="range"
          min={0}
          max={optimizerStyles.length - 1}
          step={1}
          value={shownStyle}
          disabled={!editable}
          aria-valuetext={
            style === null ? 'Custom settings' : optimizerStyles[style].label
          }
          onChange={(event) =>
            onRules(applyStyle(rules, Number(event.target.value)))
          }
        />
        <div className="optimizer-style-scale" aria-hidden="true">
          <span>Passive</span>
          <span>Balanced</span>
          <span>Aggressive</span>
        </div>
        <p className="footnote">
          {style === null
            ? 'You have changed individual settings under Fine-tune. Move the slider to apply a style to all of them.'
            : optimizerStyles[style].summary}{' '}
          Safety checks, memory headroom and your protect level always apply.
        </p>
      </div>
      {!editable && lockedReason && <p className="footnote">{lockedReason}</p>}
      {(dirty || tooFew || confirmTooLong) && editable && (
        <div className="optimizer-plan-save" role="status">
          <span>
            {tooFew
              ? 'Choose at least two models.'
              : confirmTooLong
                ? 'Confirmation time must be no longer than the minimum run.'
                : on
                  ? 'Changes apply right away; the current model keeps serving.'
                  : 'Saved for the next time you turn the optimizer on.'}
          </span>
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => {
              onDiscard();
              setEditing(false);
            }}
          >
            Discard
          </Button>
          <Button disabled={busy || tooFew || confirmTooLong} onClick={onSave}>
            Save changes
          </Button>
        </div>
      )}
      <details className="optimizer-plan-fine-tune">
        <summary>Fine-tune limits and other strategies</summary>
        <DemandSettings
          flat
          value={rules}
          onChange={onRules}
          disabled={!editable}
        />
        {children}
      </details>
    </div>
  );
}
