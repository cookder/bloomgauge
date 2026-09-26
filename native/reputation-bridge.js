// Runs only inside the official Darkbloom console. Observe its owner API
// response and keep the authenticated Request entirely inside WebKit.
(() => {
  if (
    location.origin !== 'https://console.darkbloom.dev' ||
    window !== window.top
  )
    return;
  const originalFetch = window.fetch.bind(window);
  let request = null;
  let refreshing = false;
  const send = (payload) =>
    window.webkit.messageHandlers.bloomReputation.postMessage(payload);
  const isProviders = (value) => {
    try {
      const url = new URL(value, location.href);
      return (
        url.origin === 'https://console.darkbloom.dev' &&
        url.pathname === '/api/me/providers'
      );
    } catch {
      return false;
    }
  };
  async function observe(response, requestedAt) {
    if (!response.ok) {
      send({
        status: [401, 403].includes(response.status)
          ? 'auth_required'
          : 'unavailable',
      });
      return;
    }
    try {
      const data = await response.clone().json();
      if (!Array.isArray(data.providers) || data.providers.length > 1000)
        throw Error();
      const keys = [
        'score',
        'total_jobs',
        'successful_jobs',
        'failed_jobs',
        'total_uptime_seconds',
        'avg_response_time_ms',
        'challenges_passed',
        'challenges_failed',
      ];
      send({
        status: 'ok',
        requestedAt,
        providers: data.providers.map((p) => ({
          se_public_key: p.se_public_key,
          id: p.id,
          models: Array.isArray(p.models)
            ? p.models.map((m) => (typeof m === 'string' ? m : (m?.id ?? null)))
            : null,
          trust_level: p.trust_level,
          status: p.status,
          online: p.online,
          last_heartbeat: p.last_heartbeat,
          pending_requests: p.pending_requests,
          max_concurrency: p.max_concurrency,
          backend_capacity: {
            slots: Array.isArray(p.backend_capacity?.slots)
              ? p.backend_capacity.slots.slice(0, 128).map((s) => ({
                  model: s.model,
                  state: s.state,
                  num_running: s.num_running,
                  num_waiting: s.num_waiting,
                  max_concurrency: s.max_concurrency,
                }))
              : null,
          },
          reputation: Object.fromEntries(
            keys
              .filter((k) => p.reputation?.[k] != null)
              .map((k) => [k, p.reputation[k]]),
          ),
        })),
      });
    } catch {
      send({ status: 'unavailable' });
    }
  }
  window.fetch = async function (input, init) {
    let candidate;
    try {
      candidate = new Request(input, init);
    } catch {
      return originalFetch(input, init);
    }
    const relevant = candidate.method === 'GET' && isProviders(candidate.url);
    if (relevant) request = candidate.clone();
    const requestedAt = Date.now() / 1000;
    try {
      const response = await originalFetch(input, init);
      if (relevant) void observe(response, requestedAt);
      return response;
    } catch (error) {
      if (relevant) send({ status: 'unavailable' });
      throw error;
    }
  };
  window.__bloomRefreshReputation = async () => {
    if (!request || refreshing) return;
    refreshing = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    const requestedAt = Date.now() / 1000;
    try {
      await observe(
        await originalFetch(
          new Request(request, {
            cache: 'no-store',
            signal: controller.signal,
          }),
        ),
        requestedAt,
      );
    } catch {
      send({ status: 'unavailable' });
    } finally {
      clearTimeout(timeout);
      refreshing = false;
    }
  };
})();
