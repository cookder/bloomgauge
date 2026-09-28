'use client';
import { useState } from 'react';
import {
  CircleAlert,
  CircleCheck,
  LoaderCircle,
  MapPin,
  House,
  ArrowUpRight,
} from 'lucide-react';
import {
  armingText,
  cellLabel,
  duration,
  evidenceTable,
  excursionProgress,
  homeSource,
  ledgerText,
  managerHeadline,
  managerSentence,
  withoutCircularHint,
  type ManagerView,
  type Readiness,
} from '@/lib/optimizer-manager';
import { age, money, num, shortModel } from './shared';
import { useAppNavigation } from './app-navigation';

/** Catalog display names ("Qwen 3.8 27B") when known, else the short id. */
export type ModelNames = Record<string, string> | undefined;
const labeler = (names: ModelNames) => (id: string) =>
  names?.[id] || shortModel(id);

const at = (value: number, now: number) => {
  const date = new Date(value * 1000);
  const today = date.toDateString() === new Date(now * 1000).toDateString();
  return date.toLocaleString([], {
    ...(today ? {} : { weekday: 'short' }),
    hour: 'numeric',
    minute: '2-digit',
  });
};

/** Current model and whether it is ready, with the concrete reason and since when. */
export function ReadinessLine({
  model,
  value,
  now,
  names,
  hideReason = false,
}: {
  model?: string | null;
  value: Readiness;
  now: number;
  names?: ModelNames;
  /** The reason is already the card's main sentence. */
  hideReason?: boolean;
}) {
  const label = labeler(names);
  return (
    <div className={`manager-readiness ${value.state}`}>
      <strong>{model ? label(model) : 'No model selected'}</strong>
      <small>
        {value.state === 'ready' ? (
          <CircleCheck size={14} aria-hidden="true" />
        ) : value.state === 'not-ready' ? (
          <CircleAlert size={14} aria-hidden="true" />
        ) : null}
        {value.label}
        {value.since != null && value.state === 'not-ready'
          ? ` since ${at(value.since, now)} (${duration(Math.max(0, now - value.since))})`
          : ''}
        {value.reason && value.state !== 'ready' && !hideReason
          ? ` · ${value.reason}`
          : ''}
      </small>
    </div>
  );
}

/** What the manager is doing now and why, in the backend's own words. */
export function ManagerStatusCard({
  view,
  currentModel,
  readiness,
  now,
  names,
}: {
  view: ManagerView;
  currentModel?: string | null;
  readiness: Readiness;
  now: number;
  names?: ModelNames;
}) {
  const label = labeler(names);
  const headline = managerHeadline(view, currentModel, label, now);
  const sentence = managerSentence(view);
  const decision = withoutCircularHint(view.reason);
  const homeModel = view.home?.model ?? null;
  const excursion =
    view.excursion && (!currentModel || view.excursion.target === currentModel)
      ? view.excursion
      : null;
  const progress = excursion ? excursionProgress(excursion, now) : null;
  const recovery = view.recovery;
  const blocked = view.blocked.filter((b) => b.until > now);
  const Icon =
    headline.tone === 'good'
      ? CircleCheck
      : headline.tone === 'working'
        ? LoaderCircle
        : CircleAlert;
  return (
    <section
      className={`manager-status tone-${headline.tone}`}
      aria-label="What Bloomkeeper is doing"
      role="status"
      aria-live="polite"
    >
      <div className="manager-status-head">
        <Icon
          size={18}
          aria-hidden="true"
          className={headline.tone === 'working' ? 'spin' : undefined}
        />
        <strong>{headline.title}</strong>
      </div>
      {sentence && <p className="manager-status-reason">{sentence}</p>}
      {/* While dark, the watchdog speaks first; the decision's own reason (e.g. why
          home must wait) still matters. */}
      {decision && decision !== sentence && (
        <p className="manager-status-reason secondary">{decision}</p>
      )}
      <dl className="manager-facts">
        <div>
          <dt>
            {view.pinned ? (
              <MapPin size={13} aria-hidden="true" />
            ) : (
              <House size={13} aria-hidden="true" />
            )}
            {view.pinned ? 'Your pick' : 'Home model'}
          </dt>
          <dd>
            <strong>{homeModel ? label(homeModel) : 'Not set yet'}</strong>
            <small>{homeSource(view.home, view.pinned)}</small>
            {view.home?.failedAt != null && (
              <small className="warning-text">
                Could not be restored at {at(view.home.failedAt, now)}. Pick it
                again to retry.
              </small>
            )}
          </dd>
        </div>
        <div>
          <dt>Serving now</dt>
          <dd>
            <ReadinessLine
              model={currentModel}
              value={readiness}
              now={now}
              names={names}
              hideReason={
                !!readiness.reason &&
                sentence.replace(/\.$/, '').includes(readiness.reason)
              }
            />
          </dd>
        </div>
        {excursion && progress && (
          <div>
            <dt>
              <ArrowUpRight size={13} aria-hidden="true" />
              Excursion
            </dt>
            <dd>
              <strong>
                {label(excursion.target)}
                {excursion.from ? ` · away from ${label(excursion.from)}` : ''}
              </strong>
              {excursion.reason && (
                <small>{withoutCircularHint(excursion.reason)}</small>
              )}
              <small>
                Predicted {money(progress.predicted)}/h
                {progress.realized != null
                  ? ` · so far ${money(progress.realized)}/h`
                  : ' · realized pay not measured yet'}
              </small>
              {progress.elapsed != null && (
                <small>
                  {duration(progress.elapsed)} so far · returns home when the
                  evidence fades or pay drops, by{' '}
                  {at((excursion.startedAt ?? now) + progress.maxSeconds, now)}{' '}
                  at the latest
                </small>
              )}
            </dd>
          </div>
        )}
        {recovery && (
          <div>
            <dt>Recovery</dt>
            <dd>
              <strong>
                {recovery.failedTarget
                  ? `${label(recovery.failedTarget)} did not become ready`
                  : 'A switch did not finish'}
                {recovery.at != null ? ` at ${at(recovery.at, now)}` : ''}
              </strong>
              <small>
                {recovery.previous
                  ? `Bloomkeeper restores ${label(recovery.previous)} if it doesn’t load`
                  : 'Bloomkeeper restores the last working model'}
                {recovery.attempts
                  ? ` · ${recovery.attempts} of 2 restores tried`
                  : ''}
                {recovery.interrupted ? ' · the app closed mid-switch' : ''}
              </small>
            </dd>
          </div>
        )}
        {view.watchdog?.nextAt != null && view.watchdog.nextAt > now && (
          <div>
            <dt>Next restore</dt>
            <dd>
              <strong>{at(view.watchdog.nextAt, now)}</strong>
              <small>
                {view.watchdog.attempts
                  ? `${view.watchdog.attempts} restore${view.watchdog.attempts === 1 ? '' : 's'} tried so far`
                  : 'Readiness watchdog'}
              </small>
            </dd>
          </div>
        )}
        {view.retryAt != null && view.retryAt > now && (
          <div>
            <dt>Retry</dt>
            <dd>
              <strong>{at(view.retryAt, now)}</strong>
              <small>The last move could not start; nothing was changed.</small>
            </dd>
          </div>
        )}
        {view.arming && (
          <div>
            <dt>Watching</dt>
            <dd>
              <small>
                {armingText(view.arming, view.evidence?.rows, label, now)}
              </small>
            </dd>
          </div>
        )}
        {!!blocked.length && (
          <div>
            <dt>Held back</dt>
            <dd>
              {blocked.map((b) => (
                <small key={b.model}>
                  {label(b.model)} until {at(b.until, now)} · failed to load
                </small>
              ))}
            </dd>
          </div>
        )}
        {view.ledger && (
          <div>
            <dt>Switch record</dt>
            <dd>
              <small>
                {ledgerText(view.ledger, homeModel, label, view.pinned)}
              </small>
            </dd>
          </div>
        )}
        {!excursion && view.lastExcursion?.endedAt != null && (
          <div>
            <dt>Last excursion</dt>
            <dd>
              <small>
                {label(view.lastExcursion.target)} · ended{' '}
                {at(view.lastExcursion.endedAt, now)}
                {view.lastExcursion.endReason
                  ? `: ${view.lastExcursion.endReason}`
                  : ''}
              </small>
            </dd>
          </div>
        )}
      </dl>
    </section>
  );
}

/** Public network evidence for this Mac's hardware class: estimated $/h per model. */
export function NetworkEvidencePanel({
  view,
  currentModel,
  now,
  names,
}: {
  view: ManagerView | null;
  currentModel?: string | null;
  now: number;
  names?: ModelNames;
}) {
  const label = labeler(names);
  const { mobile } = useAppNavigation();
  const [open, setOpen] = useState<boolean | null>(null);
  const evidence = view?.evidence ?? null;
  const rows = evidence ? evidenceTable(evidence, view?.home?.model) : [];
  const updated = evidence?.updatedAt;
  return (
    <details
      className="quiet-disclosure manager-evidence"
      open={open ?? !mobile}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span>Network evidence · Macs like this one</span>
        <small>
          {evidence
            ? `${cellLabel(evidence.cell)} · ${updated != null ? `updated ${age(updated, now).toLowerCase()}` : 'not updated yet'}`
            : 'Not available yet'}
        </small>
      </summary>
      {!evidence ? (
        <p className="footnote">
          Bloomkeeper hasn’t received network evidence for this Mac’s hardware
          class yet. It is built from public network counters every few minutes;
          until then the manager holds the home model.
        </p>
      ) : !rows.length ? (
        <p className="footnote">
          Too few Macs like this one are serving models right now to compare
          them. The manager holds the home model.
        </p>
      ) : (
        <>
          <ol className="manager-evidence-rows">
            {rows.map((row) => (
              <li key={row.model} className={row.home ? 'home' : undefined}>
                <div className="manager-evidence-model">
                  <strong>{label(row.model)}</strong>
                  <span className="manager-badges">
                    {row.home && <span className="manager-badge">Home</span>}
                    {row.model === currentModel && (
                      <span className="manager-badge serving">Serving</span>
                    )}
                  </span>
                </div>
                <div className="manager-evidence-rate">
                  <strong>
                    {row.usdPerHour != null
                      ? `${money(row.usdPerHour)}/h`
                      : '—'}
                  </strong>
                  <small>
                    {row.low != null && row.high != null
                      ? `${money(row.low)}–${money(row.high)}`
                      : 'No range'}
                    {!row.home && row.ratio != null
                      ? ` · ${num(row.ratio, 1)}× home`
                      : ''}
                  </small>
                </div>
                <small className="manager-evidence-why">
                  {row.providers != null
                    ? `${num(row.providers)} Mac${row.providers === 1 ? '' : 's'}${row.source === 'neighbour' ? ' (same chip, more memory)' : ''}`
                    : ''}
                  {row.home
                    ? ' · home model'
                    : row.eligible === true
                      ? ' · could be an excursion target'
                      : row.why
                        ? ` · ${row.why.replace(/\.$/, '')}`
                        : ''}
                </small>
              </li>
            ))}
          </ol>
          <p className="footnote">
            Estimated earnings per hour for a Mac of this class, from the last
            couple of hours of public network counters; the range is a 90%
            interval. Your own history decides the home model; these estimates
            only decide excursions.
            {evidence.homeUsdPerHour != null &&
              ` This Mac earns about ${money(evidence.homeUsdPerHour)}/h on its home model.`}
          </p>
        </>
      )}
    </details>
  );
}
