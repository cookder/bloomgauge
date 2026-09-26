/** Display comparisons only. The controller target always comes from its decision;
 * a large demand value or missing income estimate is never a next-pick score. */
export function comparisonOrder<
  T extends {
    model: string;
    current: boolean;
    selected: boolean;
    netGainUsd?: number | null;
  },
>(rows: T[]): T[] {
  return rows
    .filter((row) => !row.current)
    .sort(
      (a, b) =>
        Number(b.selected) - Number(a.selected) ||
        Number(Number.isFinite(b.netGainUsd)) -
          Number(Number.isFinite(a.netGainUsd)) ||
        (Number.isFinite(a.netGainUsd) && Number.isFinite(b.netGainUsd)
          ? b.netGainUsd! - a.netGainUsd!
          : 0) ||
        a.model.localeCompare(b.model),
    );
}
