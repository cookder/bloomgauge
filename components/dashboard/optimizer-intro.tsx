'use client';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from '@/components/ui/dialog';
import { introPoints, learningText } from '@/lib/optimizer-intro';

/** What the optimizer does. With onChoose it is the first-On step; without, it is read-only. */
export function OptimizerIntro({
  open,
  onClose,
  onChoose,
  protectUsdPerHour,
  learningMinutesPerDay,
}: {
  open: boolean;
  onClose: () => void;
  onChoose?: (boost: boolean) => void;
  protectUsdPerHour: number;
  learningMinutesPerDay: number;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="support-report-dialog optimizer-intro">
        <DialogTitle>What the optimizer does</DialogTitle>
        <DialogDescription>
          Once it’s on, Bloomkeeper picks which model this Mac serves, automatically.
        </DialogDescription>
        <ul className="optimizer-intro-points">
          {introPoints.map((p) => (
            <li key={p.title}>
              <strong>{p.title}.</strong> {p.text}
            </li>
          ))}
        </ul>
        <h3>Why it learns</h3>
        <p>{learningText(protectUsdPerHour, learningMinutesPerDay)}</p>
        <h3>Your options</h3>
        <p>
          The defaults are a good start. Later you can change which models it
          uses, the pace it protects, learning time and how actively it
          switches, all on the optimizer card. Every switch still has to pass
          memory, temperature and daily-limit checks, and Manual stops it at any
          time.
        </p>
        {onChoose ? (
          <>
            <div className="optimizer-intro-boost">
              <strong>
                New to Bloomkeeper on this Mac? Add a 3-day Learning boost.
              </strong>
              <p>
                For three days Bloomkeeper measures more models, more often, so it
                learns this Mac’s earnings faster. With Darkbloom 0.9.9 or
                later, requests in progress finish before a switch. The boost
                ends by itself.
              </p>
            </div>
            <div className="optimizer-intro-actions">
              <button
                type="button"
                className="action"
                onClick={() => onChoose(true)}
              >
                Turn on with 3-day boost
              </button>
              <button
                type="button"
                className="secondary-button"
                onClick={() => onChoose(false)}
              >
                Turn on without boost
              </button>
              <button type="button" className="text-link" onClick={onClose}>
                Not now
              </button>
            </div>
          </>
        ) : (
          <div className="optimizer-intro-actions">
            <button type="button" className="action" onClick={onClose}>
              Got it
            </button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
