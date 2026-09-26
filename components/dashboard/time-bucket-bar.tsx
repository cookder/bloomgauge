import type { BarShapeProps } from 'recharts';

/** Size by elapsed time, not by the nearest pair of timestamps. A partial
 * current hour or forecast anchor must not shrink every historical bar. */
export function TimeBucketBar({
  x,
  y,
  width,
  height,
  fill,
  fillOpacity,
  stroke,
  strokeOpacity,
  strokeDasharray,
  parentViewBox,
  spanSeconds,
  bucketSeconds = 3600,
}: Partial<BarShapeProps> & { spanSeconds: number; bucketSeconds?: number }) {
  if (x == null || y == null || width == null || !height || !parentViewBox)
    return null;
  const barWidth = Math.min(
    24,
    (parentViewBox.width * bucketSeconds * 0.72) /
      Math.max(bucketSeconds, spanSeconds),
  );
  return (
    <rect
      x={x + width / 2 - barWidth / 2}
      y={Math.min(y, y + height)}
      width={barWidth}
      height={Math.abs(height)}
      fill={fill}
      fillOpacity={fillOpacity}
      stroke={stroke}
      strokeOpacity={strokeOpacity}
      strokeDasharray={strokeDasharray}
    />
  );
}
