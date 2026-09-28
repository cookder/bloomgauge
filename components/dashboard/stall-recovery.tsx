'use client';
import { stallView } from '@/lib/stall-recovery';
import { shortModel } from './shared';

const time = (at: number) =>
  new Date(at * 1000).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
  });
const day = (at: number, now: number) =>
  new Date(at * 1000).toDateString() === new Date(now * 1000).toDateString()
    ? time(at)
    : new Date(at * 1000).toLocaleString([], {
        weekday: 'short',
        hour: 'numeric',
        minute: '2-digit',
      });

/** Shown while work has stalled, and afterwards until work resumes or a model is ready again (a day at most). */
export function StallRecovery({
  value,
  events,
  now,
  manager = false,
}: {
  value: unknown;
  events: unknown;
  now: number;
  /** The manager holds the home model; its ladder has no "Try another model". */
  manager?: boolean;
}) {
  const view = stallView(value, events, now, { manager });
  if (!view) return null;
  const active = view.state === 'active';
  return (
    <section
      className={`stall-recovery ${active ? 'active' : ''}`}
      aria-label="Stall recovery"
      role={active ? 'status' : undefined}
    >
      <div className="stall-recovery-head">
        <strong>
          {active ? 'Work stopped' : 'Recent stall'}
          {view.model && <> · {shortModel(view.model)}</>}
        </strong>
        {view.since != null && (
          <small>
            {active ? 'since' : 'at'} {day(view.since, now)}
          </small>
        )}
      </div>
      <p>
        {view.headline}
        {view.detail && <> {view.detail}</>}
      </p>
      <ol className="stall-steps">
        {view.steps.map((s) => (
          <li key={s.step} className={s.state}>
            <span className="stall-step-label">{s.label}</span>
            <small>
              {s.state === 'done'
                ? `${time(s.at!)}${s.note ? ` · ${s.note}` : ''}`
                : s.state === 'skipped'
                  ? 'Skipped'
                  : active
                    ? 'If work doesn’t resume'
                    : 'Not needed'}
            </small>
          </li>
        ))}
      </ol>
    </section>
  );
}
