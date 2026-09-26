export type TrafficWindow = {
  tokensPerSecond: number | null;
  requestsPerMinute: number | null;
  outputTokens: number | null;
  requests: number | null;
  seconds: number;
  start: number;
  end: number;
};
export type TrafficPulseData = {
  at: number;
  updatedAt: number | null;
  sessionId: number | null;
  models: string[];
  status: string;
  detail: string;
  windows: Record<string, TrafficWindow>;
  baseline: {
    tokensPerSecond: number | null;
    requestsPerMinute: number | null;
    seconds: number;
    tokensSeconds: number;
    requestsSeconds: number;
    scope: string;
  } | null;
};

export function trafficRate(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

export function trafficReading(
  traffic: TrafficPulseData | undefined,
  metric: 'tokens' | 'requests',
) {
  if (!traffic) return undefined;
  const field = metric === 'tokens' ? 'tokensPerSecond' : 'requestsPerMinute';
  return {
    at: traffic.at,
    streamId: `traffic:${traffic.sessionId}:${traffic.models.join('|')}`,
    sessionId: traffic.sessionId,
    status: traffic.status,
    models: traffic.models,
    windows: Object.fromEntries(
      Object.entries(traffic.windows).map(([key, value]) => [
        key,
        { rate: trafficRate(value[field]) },
      ]),
    ),
  };
}
