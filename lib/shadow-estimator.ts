// GET /api/optimizer/shadow-estimator (native/demand_curve_journal.py): the
// pressure-curve estimator running in shadow. Display only; switching never uses it.

const record = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);
const nullableNumber = (v: unknown) => v === null || finite(v);

// Mirrors READY_DAYS / READY_WINDOWS in native/demand_curve_journal.py.
const SHADOW_READY_DAYS = 7;
const SHADOW_READY_WINDOWS = 100;

export type ShadowPrediction = {
  basis: string;
  hours: number;
  periods: number;
  pressure: number | null;
  serving: boolean;
  usdPerHour: number | null;
  lower: number | null;
  upper: number | null;
  extrapolated: boolean;
};
export type ShadowScore = {
  windows: number;
  pairedWindows: number;
  pendingWindows: number;
  daysScored: number;
  curveMAE: number | null;
  pairedCurveMAE: number | null;
  pairedMatchedMAE: number | null;
  inRangeShare: number | null;
};
export type ShadowReport = {
  latest: {
    at: number;
    current: string | null;
    models: Record<string, ShadowPrediction>;
  } | null;
  evaluation: {
    status: string;
    overall?: ShadowScore;
    leader?: 'curve' | 'matched' | null;
  };
};

function prediction(v: unknown): v is ShadowPrediction {
  return (
    record(v) &&
    typeof v.basis === 'string' &&
    finite(v.hours) &&
    finite(v.periods) &&
    typeof v.serving === 'boolean' &&
    typeof v.extrapolated === 'boolean' &&
    ['pressure', 'usdPerHour', 'lower', 'upper'].every((k) =>
      nullableNumber(v[k]),
    ) &&
    (v.usdPerHour === null || (finite(v.lower) && finite(v.upper)))
  );
}
function score(v: unknown): v is ShadowScore {
  return (
    record(v) &&
    ['windows', 'pairedWindows', 'pendingWindows', 'daysScored'].every(
      (k) => finite(v[k]) && (v[k] as number) >= 0,
    ) &&
    ['curveMAE', 'pairedCurveMAE', 'pairedMatchedMAE', 'inRangeShare'].every(
      (k) => nullableNumber(v[k]),
    )
  );
}

export function validShadowReport(v: unknown): v is ShadowReport {
  if (
    !record(v) ||
    !record(v.evaluation) ||
    typeof v.evaluation.status !== 'string'
  )
    return false;
  const e = v.evaluation;
  if (e.overall != null && !score(e.overall)) return false;
  if (e.leader != null && e.leader !== 'curve' && e.leader !== 'matched')
    return false;
  if (v.latest === null) return true;
  const l = v.latest;
  return (
    record(l) &&
    finite(l.at) &&
    (l.current == null || typeof l.current === 'string') &&
    record(l.models) &&
    Object.keys(l.models).length <= 256 &&
    Object.values(l.models).every(prediction)
  );
}

/** One line on how far the shadow test has come and which estimator is ahead so far. */
export function shadowProgress(
  report: ShadowReport,
  money: (n: number, d?: number) => string,
): string {
  const e = report.evaluation;
  if (e.status === 'disabled') return 'The shadow test is off on this Mac.';
  const o = e.overall;
  if (!o || !o.pairedWindows)
    return `Scoring starts once predictions are 30 minutes old and a model has run steadily through them. It needs ${SHADOW_READY_DAYS} scored days and ${SHADOW_READY_WINDOWS} compared half-hours.`;
  const progress = `${Math.min(o.pairedWindows, SHADOW_READY_WINDOWS)} of ${SHADOW_READY_WINDOWS} compared half-hours, ${Math.min(o.daysScored, SHADOW_READY_DAYS)} of ${SHADOW_READY_DAYS} days`;
  const misses =
    finite(o.pairedCurveMAE) && finite(o.pairedMatchedMAE)
      ? ` Typical miss so far: ${money(o.pairedCurveMAE)}/hr shadow vs ${money(o.pairedMatchedMAE)}/hr current method${e.leader === 'curve' ? ' (shadow ahead)' : e.leader === 'matched' ? ' (current method ahead)' : ''}.`
      : '';
  return e.status === 'ready'
    ? `Enough scoring to review: ${progress}.${misses} It only takes over if it proves more accurate.`
    : `Scored ${progress}.${misses}`;
}
