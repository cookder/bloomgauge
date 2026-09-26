'use client';
import { modelColor } from '@/lib/model-earnings';
import {
  forecastReading,
  type EarningsOutlookModel,
  type ForecastEvaluation,
} from '@/lib/earnings-outlook';
import { money, num, shortModel, plural } from './shared';

function forecastMoney(rate: number | null) {
  if (rate != null && rate > 0 && rate < 0.001) return '<$0.001';
  if (rate != null && rate < 0 && rate > -0.001) return '−<$0.001';
  return money(rate);
}
const time = (at: number | null | undefined) =>
  at == null
    ? 'Unknown'
    : new Date(at * 1000).toLocaleTimeString([], {
        hour: 'numeric',
        minute: '2-digit',
      });
const date = (at: number | null | undefined) =>
  at == null
    ? 'Unknown'
    : new Date(at * 1000).toLocaleString([], {
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      });
export function EarningsForecastView({
  models,
  selected,
  onSelect,
  now,
  stale,
  evaluation,
  persistence,
  onHistory,
}: {
  models: EarningsOutlookModel[];
  selected: EarningsOutlookModel | undefined;
  onSelect: (model: string) => void;
  now: number;
  stale: boolean;
  evaluation?: ForecastEvaluation;
  persistence?: string;
  onHistory: () => void;
}) {
  const selectedReading = selected
    ? forecastReading(selected, now, stale)
    : null;
  const f = selectedReading?.forecast;
  const score = evaluation?.models.find((m) => m.model === selected?.model);
  const max = Math.max(
    0.001,
    ...models.map((m) => Math.max(0, forecastReading(m, now, stale).rate ?? 0)),
  );
  return (
    <>
      <p className="outlook-intro">
        An experimental one-hour baseline for each model, if already warm and
        kept ready on this Mac. Recent paid pace and whole historical hours
        include quiet periods. These estimates do not yet predict changes in
        demand.
      </p>
      <div className="outlook-body forecast-body">
        <div className="outlook-comparison">
          <div
            className="outlook-models"
            aria-label="Next-hour model forecasts"
          >
            {models.map((model) => {
              const r = forecastReading(model, now, stale),
                p = r.forecast;
              return (
                <button
                  key={model.model}
                  type="button"
                  className={`outlook-model ${r.rate != null ? 'supported' : 'limited'}`}
                  aria-pressed={selected?.model === model.model}
                  onClick={() => onSelect(model.model)}
                >
                  <span className="outlook-model-name">
                    <i style={{ background: modelColor(model.model) }} />
                    <strong>{shortModel(model.model)}</strong>
                    {model.serving && <small>Serving</small>}
                  </span>
                  <span className="outlook-rate">
                    <strong>{forecastMoney(r.rate)}</strong>
                    <small> / hr</small>
                  </span>
                  <span className="outlook-bar" aria-hidden="true">
                    <i
                      style={{
                        width: `${r.rate == null ? 0 : (Math.max(0, r.rate) / max) * 100}%`,
                        background: modelColor(model.model),
                      }}
                    />
                  </span>
                  <span className="outlook-quality">
                    {r.label}
                    {!model.eligible && !model.serving
                      ? ' · Not currently eligible'
                      : ''}
                  </span>
                  <span className="outlook-evidence">
                    {p?.origin != null
                      ? `${time(p.origin)}–${time(r.end)} · `
                      : ''}
                    {p
                      ? `${plural(p.support.completedHours, 'complete hour')} · ${plural(p.support.distinctDates, 'date')}`
                      : 'Awaiting forecast evidence'}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
        {selected && selectedReading && (
          <div
            className="outlook-focus forecast-focus"
            tabIndex={-1}
            aria-label="Selected model forecast"
          >
            <div className="outlook-focus-heading">
              <div>
                <span className="eyebrow">ONE-HOUR OUTLOOK</span>
                <h3>{shortModel(selected.model)}</h3>
              </div>
              <strong style={{ color: modelColor(selected.model) }}>
                {forecastMoney(selectedReading.rate)}
                <small> / hr</small>
              </strong>
            </div>
            <div
              className={`forecast-status ${selectedReading.saved || selectedReading.expired ? 'saved' : ''}`}
            >
              {selectedReading.label}
            </div>
            {f?.origin != null && (
              <p className="forecast-window">
                {selectedReading.upcoming ? 'Starts ' : ''}
                {time(f.origin)}–{time(selectedReading.end)} ·{' '}
                {new Date(f.origin * 1000).toLocaleDateString([], {
                  month: 'short',
                  day: 'numeric',
                })}{' '}
                · local time
              </p>
            )}
            <p>
              {selectedReading.expired
                ? 'This forecast’s hour has ended. Waiting for a new forecast; the old value is not extended into a new hour.'
                : selectedReading.reason}
            </p>
            {selectedReading.saved && (
              <p className="notice">
                Showing the saved forecast for its original time window while
                fresh readings return.
              </p>
            )}
            {f && (
              <>
                <p className="forecast-assumption">{f.assumption}</p>
                {f.uncertainty.lower != null && f.uncertainty.upper != null && (
                  <div className="forecast-spread">
                    <span>Historical hourly spread</span>
                    <strong>
                      {forecastMoney(f.uncertainty.lower)}–
                      {forecastMoney(f.uncertainty.upper)} <small>/ hr</small>
                    </strong>
                    <p>{f.uncertainty.label}</p>
                  </div>
                )}
                <details className="outlook-details">
                  <summary>Evidence behind this outlook</summary>
                  <dl className="forecast-evidence-grid">
                    <div>
                      <dt>Complete hours</dt>
                      <dd>{num(f.support.completedHours)}</dd>
                    </div>
                    <div>
                      <dt>Recorded dates</dt>
                      <dd>{num(f.support.distinctDates)}</dd>
                    </div>
                    <div>
                      <dt>Demand observed</dt>
                      <dd>{date(f.demandAsOf)}</dd>
                    </div>
                    <div>
                      <dt>Paid history through</dt>
                      <dd>{date(f.paidEvidenceThrough)}</dd>
                    </div>
                  </dl>
                  <p className="small muted">
                    {f.basis === 'recent_paid_persistence'
                      ? `Based on ${f.support.recentCompleteMinutes} complete paid minutes, including verified idle time.`
                      : 'Based on complete historical hours on this Mac, without an unproven demand multiplier.'}
                  </p>
                  <div className="forecast-accuracy">
                    <strong>Accuracy tracking</strong>
                    <p>
                      {score?.windows
                        ? `${num(score.windows)} completed forecast hours · average absolute error ${forecastMoney(score.maeUSDPerHour)} / hr.`
                        : persistence === 'versioned_local_journal'
                          ? 'Forecasts are being saved before their outcomes. No complete scored hour for this model yet.'
                          : 'Accuracy tracking is starting; no scored hours yet.'}
                    </p>
                    {score != null && (
                      <p>
                        {num(score.censoredWindows)} interrupted or incomplete
                        hours excluded · {num(score.pendingWindows)} awaiting
                        outcomes. Unselected models have no assumed earnings.
                      </p>
                    )}
                    <p>
                      {evaluation?.creditOutcomeStatus ??
                        'Later credit corrections can change scored outcomes; the original forecast stays fixed.'}
                    </p>
                  </div>
                  <p className="small muted">
                    {f.validation.retrospectiveEvidence}
                  </p>
                  <p className="small muted">
                    {f.validation.creditAvailability}
                  </p>
                  {f.historicalFallback.usdPerWarmHour != null &&
                    selectedReading.rate == null && (
                      <p className="small muted">
                        Historical observation:{' '}
                        {forecastMoney(f.historicalFallback.usdPerWarmHour)} /
                        warm hr across {num(f.historicalFallback.observedHours)}{' '}
                        complete hours. This is not a supported forecast.
                      </p>
                    )}
                </details>
              </>
            )}
            <button type="button" className="text-link" onClick={onHistory}>
              Explore observed earnings by demand →
            </button>
          </div>
        )}
      </div>
      <div className="outlook-optimizer-note">
        <strong>Forecasts are being evaluated</strong>
        <p>
          These estimates are advisory. The optimizer continues to use its
          existing paid-work checks; a new forecast must prove useful against
          actual future earnings before it can guide automatic switches.
        </p>
      </div>
    </>
  );
}
