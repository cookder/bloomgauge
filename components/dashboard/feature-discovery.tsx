'use client';
import { useEffect, useRef, useState } from 'react';
import { Smartphone, Sparkles } from 'lucide-react';
import { startChartPolling } from '@/lib/chart-polling';
import { usePageVisible } from '@/lib/use-page-visibility';
import { requestFeatureSetup } from '@/lib/feature-discovery-intent';
import { useAppNavigation } from './app-navigation';

type Feature = {
  id: 'phone' | 'optimizer';
  destination: 'phone' | 'optimizer';
};
type Discovery = {
  schema: 1;
  available: boolean;
  localOnly: boolean;
  features: Feature[];
};
function valid(value: unknown): value is Discovery {
  const data = value as Discovery | null;
  return (
    !!data &&
    data.schema === 1 &&
    typeof data.available === 'boolean' &&
    typeof data.localOnly === 'boolean' &&
    Array.isArray(data.features) &&
    data.features.length <= 2 &&
    new Set(data.features.map((feature) => feature?.id)).size ===
      data.features.length &&
    data.features.every(
      (feature) =>
        feature &&
        (feature.id === 'phone'
          ? feature.destination === 'phone' && !data.localOnly
          : feature.id === 'optimizer' && feature.destination === 'optimizer'),
    )
  );
}

export function FeatureDiscovery({
  healthy,
  active,
}: {
  healthy: boolean;
  active: boolean;
}) {
  const { navigate } = useAppNavigation();
  const pageVisible = usePageVisible();
  const [data, setData] = useState<Discovery | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const epoch = useRef(0);
  useEffect(() => {
    if (!healthy || !active || !pageVisible) {
      setData(null);
      return;
    }
    return startChartPolling({
      load: async (signal) => {
        const own = epoch.current;
        const response = await fetch('/api/discovery', {
          cache: 'no-store',
          signal,
        });
        if (!response.ok) throw Error('Feature suggestions are unavailable.');
        const value: unknown = await response.json();
        if (!valid(value)) throw Error('Feature suggestions are unavailable.');
        return { own, value };
      },
      onValue: ({ own, value }) => {
        if (!pending.current && own === epoch.current) setData(value);
      },
      onError: () => setData(null),
      intervalMs: 15_000,
      timeoutMs: 10_000,
    });
  }, [healthy, active, pageVisible]);

  async function act(feature: Feature, action: 'open' | 'snooze' | 'dismiss') {
    if (pending.current) return;
    pending.current = true;
    epoch.current++;
    setBusy(true);
    setError('');
    try {
      const response = await fetch('/api/discovery', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Bloom-Action': 'discovery',
        },
        body: JSON.stringify({ action, feature: feature.id }),
        signal: AbortSignal.timeout(10_000),
      });
      const value: unknown = await response.json();
      if (!response.ok || !valid(value))
        throw Error(
          'Could not save this choice. Try again before closing Bloomkeeper.',
        );
      setData(value);
      if (action === 'open') {
        if (feature.destination === 'phone') navigate('access');
        else {
          requestFeatureSetup();
          navigate('test');
        }
      }
    } catch (failure) {
      setError(
        failure instanceof Error
          ? failure.message
          : 'Could not save this choice.',
      );
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  if (
    !healthy ||
    !active ||
    !pageVisible ||
    !data?.available ||
    !data.features.length
  )
    return null;
  return (
    <section className="feature-discovery" aria-label="Optional next steps">
      <div className="feature-discovery-heading">
        <strong>A couple of hours in. Explore when you’re ready.</strong>
        <span>Optional · your current setup keeps running</span>
      </div>
      {data.features.map((feature) => {
        const phone = feature.id === 'phone';
        const title = phone
          ? 'Keep your Mac in view from your phone'
          : 'Set up your optimizer';
        return (
          <div className="feature-discovery-row" key={feature.id}>
            {phone ? (
              <Smartphone size={18} aria-hidden="true" />
            ) : (
              <Sparkles size={18} aria-hidden="true" />
            )}
            <div className="feature-discovery-copy">
              <h2>{title}</h2>
              <p>
                {phone
                  ? 'View earnings and use manual controls with your own Tailscale connection.'
                  : 'Review the strategy and models first. Automation starts only when you enable it.'}
              </p>
              <div className="feature-discovery-actions">
                <button
                  type="button"
                  className="text-link"
                  disabled={busy}
                  onClick={() => void act(feature, 'open')}
                >
                  {phone ? 'Set up phone access' : 'Review optimizer setup'}{' '}
                  <span aria-hidden="true">→</span>
                </button>
                <button
                  type="button"
                  className="text-link muted"
                  disabled={busy}
                  onClick={() => void act(feature, 'snooze')}
                >
                  Later · 24 hours
                </button>
                <button
                  type="button"
                  className="text-link muted"
                  disabled={busy}
                  onClick={() => void act(feature, 'dismiss')}
                  aria-label={`Don't show ${phone ? 'phone' : 'optimizer'} suggestion again`}
                  title="Don't show this suggestion again"
                >
                  Don’t show again
                </button>
              </div>
            </div>
          </div>
        );
      })}
      {error && (
        <p className="footnote" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
