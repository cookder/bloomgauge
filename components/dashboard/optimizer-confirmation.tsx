import {
  confirmationPresentation,
  type OptimizerConfirmation,
} from '@/lib/optimizer-live';
import { shortModel } from './shared';

export function ConfirmationProgress({
  value,
  now,
}: {
  value: OptimizerConfirmation;
  now: number;
}) {
  const display = confirmationPresentation(value, now);
  return (
    <div
      className={`demand-confirmation confirmation-${display.state}`}
      role="status"
    >
      <strong>
        {shortModel(value.model)} · {display.label}
      </strong>
      <span>{display.progress} confirmed observations</span>
      <progress
        aria-label="Earnings confirmation"
        max={value.requiredSeconds}
        value={display.seconds}
      />
      <small>{display.detail}</small>
    </div>
  );
}
